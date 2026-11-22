import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SYSADMIN = Path(__file__).resolve().parent.parent
TARGET = (SYSADMIN / "install/target_setup.sh").read_text(encoding="utf-8")
DEPLOY = (SYSADMIN / "install/deploy_target.sh").read_text(encoding="utf-8")

def test_mysql_grants_only_the_inventorys_tables_and_keeps_password():
    # the tables are the deployment's (mysql_tables of the overlay's inventory), checked before any SQL
    assert "--mysql-tables) mysql_tables=$2; shift 2 ;;" in TARGET and "for table in $mysql_tables; do" in TARGET
    assert "[[ $mysql_tables =~ ^[A-Za-z0-9_]+( [A-Za-z0-9_]+)*$ ]]" in TARGET
    assert "get('mysql_tables', [])" in DEPLOY and "--mysql-tables '$tables'" in DEPLOY
    assert "lock_wait_timeout=10" in TARGET
    # a repeated run: the password is taken from .my.cnf if it exists and the user agent_ro is already created
    assert """if [ -f "$cnf" ] && mysql_root -N -B -e "SELECT 1 FROM mysql.user WHERE user='agent_ro' AND host='localhost'" | grep -qx 1; then""" in TARGET
    assert r"""pw=$(sed -n 's/^password="\(.*\)"$/\1/p' "$cnf")""" in TARGET
    # a new password is generated only in the else branch
    assert TARGET.index("secrets.choice") > TARGET.index('if [ -z "$pw" ]; then', TARGET.index("sed -n 's/^password="))


def test_agent_ro_key_is_restricted_to_gate_and_stage_ip():
    # the factory host comes from host.json through deploy_target.sh, never a literal in the code
    assert "--stage-ip) stage_ip=$2; shift 2 ;;" in TARGET and "--stage-alias) stage_alias=$2; shift 2 ;;" in TARGET
    assert '[[ $stage_ip =~ ^[0-9]{1,3}(\\.[0-9]{1,3}){3}$ ]]' in TARGET
    assert "--stage-ip $STAGE_IP --stage-alias $STAGE" in DEPLOY
    assert 'STAGE=$(host_value stage_alias) && STAGE_IP=$(host_value stage_ip)' in DEPLOY
    assert "restrict,command=\"%s\",from=\"%s\" %s" in TARGET
    assert "visudo -c -f" in TARGET


def test_authorized_keys_are_never_rewritten_in_place():
    # a copy alongside, assembly in a temporary file in the same directory, a check of the line count, an atomic mv
    assert 'cp -a "$file" "$file.bak-kd-' in TARGET
    assert 'mv -f "$tmp" "$file"' in TARGET
    assert '[ "$rc" -le 1 ]' in TARGET
    assert "> \"$exec_auth\"" not in TARGET and "cat \"$tmp\" > \"$file\"" not in TARGET


def test_target_has_read_only_preflight():
    assert "--check) mode=check" in TARGET
    assert "sshd -T -C user=agent-ro" in TARGET


def test_deploy_ships_committed_files_verified_by_sha256_and_pins_host_keys():
    assert "git show \"HEAD:$1\"" in DEPLOY
    assert "sha256sum" in DEPLOY
    assert "ssh-keygen -F \"$host_ip\"" in DEPLOY
    assert "/etc/digipaddock/servers.enabled" in DEPLOY
    # inventory.json has Cyrillic: on Windows the default encoding is cp1251, so read bytes and decode as UTF-8
    assert "sys.stdin.buffer.read().decode('utf-8')" in DEPLOY


# --- root does not write into directories the agent writes to (/home/digi/**): only runuser -u digi, atomically ---

AGENT_HOME = r"/home/digi(?![\w-])"
RUNUSER_BLOCK = re.compile(r"runuser -u digi -- bash -euc '.*?'", re.S)
RUNUSER_LINE = re.compile(r"runuser -u digi -- [^\n]*")
WRITE_OPS = re.compile(
    r"\binstall\b.*-o digi\b|\bchown\b|\bchmod\b|\brm\b|\bmkdir\b|\btouch\b|\bmv\b|\bcp\b|\btee\b|\bln\b|>>?\s*\S*" + AGENT_HOME
)


def root_lines(script):
    """The script's lines outside runuser -u digi calls and outside comments."""
    text = RUNUSER_BLOCK.sub("", script)
    text = RUNUSER_LINE.sub("", text)
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def test_root_never_writes_inside_agent_home():
    for name, script in (("deploy_target.sh", DEPLOY),):
        for line in root_lines(script):
            if re.search(AGENT_HOME, line) and WRITE_OPS.search(line):
                raise AssertionError(f"{name}: root пишет в /home/digi вне runuser: {line.strip()}")


def test_root_never_writes_through_variable_holding_agent_home_path():
    for name, script in (("deploy_target.sh", DEPLOY),):
        lines = root_lines(script)
        names = {m.group(1) for ln in lines for m in re.finditer(r"\b(\w+)=\S*" + AGENT_HOME, ln)}
        for var in names:
            ref = r"\$" + var + r"\b|\$\{" + var + r"\}"
            for line in lines:
                if re.search(ref, line) and (WRITE_OPS.search(line) or re.search(r">>?\s*\"?(" + ref + ")", line)):
                    raise AssertionError(f"{name}: root пишет через ${var} (путь в /home/digi): {line.strip()}")


def test_variable_check_catches_a_mutation():
    bad = 'd=/home/digi/.ssh\nchmod 0700 "$d"\necho x > "$d/k"\n'
    names = {m.group(1) for m in re.finditer(r"\b(\w+)=\S*" + AGENT_HOME, bad)}
    assert names == {"d"}
    assert WRITE_OPS.search('chmod 0700 "$d"')


def test_deploy_writes_agent_known_hosts_as_agent_atomically():
    assert 'runuser -u "$user" -- sh -c' in DEPLOY and "d=/home/$3/.ssh; f=$d/known_hosts" in DEPLOY
    # stage's names from its naming.py, checked locally before any command
    assert "SYSADMIN_USER=$(stage_user agent sysadmin) && STAGE_EXEC_USER=$(stage_user exec)" in DEPLOY
    assert "[[ $SYSADMIN_USER =~ $name_re && $STAGE_EXEC_USER =~ $name_re ]]" in DEPLOY
    assert "_ $host_ip $target $SYSADMIN_USER $STAGE_EXEC_USER" in DEPLOY
    assert 'mktemp "$d/.known_hosts.XXXXXX"' in DEPLOY
    assert 'mv -f "$new" "$f"' in DEPLOY
    # the exec user: root writes into its home, atomically (the agent cannot reach its home)
    assert 'd=/home/$exec_user/.ssh; f=$d/known_hosts' in DEPLOY
    assert 'chown "$exec_user:$exec_user" "$new"' in DEPLOY


# --- round 1 of the security review ---

def test_deploy_validates_keys_locally_before_any_target_use():
    assert 'ro_re="^ssh-ed25519 [A-Za-z0-9+/]+={0,2} agent-ro@$STAGE\\$"' in DEPLOY
    assert 'exec_re="^ssh-ed25519 [A-Za-z0-9+/]+={0,2} agent-exec@$STAGE\\$"' in DEPLOY
    assert 'runuser -u "$SYSADMIN_USER" -- cat "/home/$SYSADMIN_USER/.ssh/agent_ro_ed25519.pub"' in DEPLOY
    first_target_ssh = DEPLOY.index('ssh "$target"')
    assert DEPLOY.index('[[ $ro_key =~ $ro_re ]]') < first_target_ssh
    assert DEPLOY.index('[[ $exec_key =~ $exec_re ]]') < first_target_ssh
    assert DEPLOY.index("ro_key=$(") < DEPLOY.index('[[ $ro_key =~ $ro_re ]]')


def _bash_exe():
    import shutil
    for c in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")):
        if c and Path(c).exists():
            return c
    return None


def test_key_regex_rejects_injection_values():
    import subprocess
    import pytest
    bash = _bash_exe()
    if bash is None:
        pytest.skip("нет bash")
    line = re.search(r'ro_re="([^"]+)"', DEPLOY).group(1)
    script = f'STAGE=example-stage; ro_re="{line}"; [[ $1 =~ $ro_re ]]'
    good = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc+/= agent-ro@example-stage"  # privacy-scan: allow (fake sample)
    assert subprocess.run([bash, "-c", script, "_", good]).returncode == 0
    for bad in (good + "'", good + " ; id", good.replace("abc", "$(id)"), good + "\nssh-ed25519 AAAA x@y",
                good.replace("agent-ro@example-stage", "agent-ro@example-stage'; id; '"), "x" + good):
        assert subprocess.run([bash, "-c", script, "_", bad]).returncode != 0, repr(bad)


EXEC_BLOB = "AAAAC3NzaC1lZDI1NTE5AAAAIExecKeyExecKeyExecKeyExecKeyExecKeyExecK"  # privacy-scan: allow (fake sample)
EXEC_PUB = f"ssh-ed25519 {EXEC_BLOB} agent-exec@example-stage"


def _exec_key_install(tmp_path, keys):
    """The install step 5 of target_setup.sh (edit_keys, key_blob and the exec line guard) on a scratch file."""
    import subprocess
    import pytest
    bash = _bash_exe()
    if bash is None or os.name != "posix":
        pytest.skip("нет bash")
    functions = TARGET.split("# --- edit_keys\n", 1)[1].split("# --- end edit_keys\n", 1)[0]
    step = TARGET.split("exec_line=", 1)[1].split("\nfi\n", 1)[0]
    auth = tmp_path / "authorized_keys"
    auth.write_text(keys, encoding="utf-8")
    script = ("set -euo pipefail\n" + functions + f'EXEC_GATE=/usr/local/bin/agent-exec-gate STAGE_IP=203.0.113.10\n'
              f'EXEC_COMMENT=agent-exec@example-stage exec_key="{EXEC_PUB}" exec_auth="{auth}"\n'
              "exec_line=" + step + "\nfi\n")
    proc = subprocess.run([bash, "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return auth.read_text(encoding="utf-8").splitlines(), sorted(p.name for p in tmp_path.glob("authorized_keys.bak-kd-*"))


GATE_LINE = f'restrict,command="/usr/local/bin/agent-exec-gate",from="203.0.113.10" {EXEC_PUB}'


def test_exec_key_lines_go_by_comment_and_by_blob(tmp_path):
    # the same key under another comment (or none) would log in past the gate — removed by its blob too;
    # other people's keys stay
    other = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherOtherOtherOtherOtherOtherOtherOther backup@mirror"  # privacy-scan: allow (fake sample)
    lines, backups = _exec_key_install(tmp_path, "\n".join([
        other, f"from=\"203.0.113.10\" ssh-ed25519 {EXEC_BLOB}", f"ssh-ed25519 {EXEC_BLOB} renamed@host",
        f'restrict,from="203.0.113.10" {EXEC_PUB}']) + "\n")
    assert lines == [other, GATE_LINE] and len(backups) == 1


def test_exec_key_file_is_untouched_when_the_gate_line_is_its_only_line(tmp_path):
    other = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCother root@laptop"  # privacy-scan: allow (fake sample)
    lines, backups = _exec_key_install(tmp_path, f"{other}\n{GATE_LINE}\n")
    assert lines == [other, GATE_LINE] and backups == []
    # the gate's line is there, but a second line of the same key is too: rewritten
    lines, backups = _exec_key_install(tmp_path, f"{GATE_LINE}\nssh-ed25519 {EXEC_BLOB}\n")
    assert lines == [GATE_LINE] and len(backups) == 1


def test_remove_passes_the_exec_key_so_its_lines_go_by_blob():
    # The rollback removed the exec key's lines by comment only — deploy_target.sh now reads the key
    # on the stage for --remove too (the same exec_re) and passes it; without it, by comment, with a warning
    remove = DEPLOY.split('if [ "$mode" = --remove ]; then', 1)[1].split("\nfi\n", 1)[0]
    assert '[[ $remove_key =~ $exec_re ]]' in remove and "remove_args=\"--exec-key '$remove_key'\"" in remove
    assert "target_setup.sh --remove --exec-user $exec_user $remove_args" in remove
    target_remove = TARGET.split('if [ "$mode" = remove ]; then', 1)[1].split("\nfi\n", 1)[0]
    assert 'exec_blob=$(key_blob "$exec_key")' in target_remove
    assert 'edit_keys "$exec_auth" "" "$EXEC_COMMENT" "$exec_blob"' in target_remove


def test_key_blob_reads_only_a_well_formed_key():
    assert 'key_blob "$exec_key"' in TARGET
    assert "[[ $exec_key =~ ^ssh-ed25519\\ [A-Za-z0-9+/]+={0,2}\\ agent-exec@[A-Za-z0-9-]+$" in TARGET


def test_agent_ro_home_is_root_owned_and_symlinks_refused():
    assert "chown root:root /home/agent-ro" in TARGET
    assert "chmod 0755 /home/agent-ro" in TARGET
    assert TARGET.index("useradd --create-home") < TARGET.index("chown root:root /home/agent-ro")
    assert '[ -L "$p" ]' in TARGET
    for path in ("/home/agent-ro/.ssh", "/home/agent-ro/.ssh/authorized_keys", "/home/agent-ro/.my.cnf"):
        assert path in TARGET.split("refuse_symlinks() {", 1)[1].split("\n}\n", 1)[0]
    remove_block = TARGET.split('if [ "$mode" = remove ]; then', 1)[1].split("\nfi\n", 1)[0]
    assert remove_block.index("refuse_symlinks") < remove_block.index("rm -f /home/agent-ro/.ssh/authorized_keys")
    start = TARGET.index("\nrefuse_symlinks\ninstall -d -o root -g root -m 0755 /home/agent-ro/.ssh")
    install_part = TARGET[start:]
    assert install_part.index("refuse_symlinks") < install_part.index("authorized_keys")
    assert install_part.count("refuse_symlinks") >= 2  # before .ssh and before .my.cnf
    # authorized_keys is written atomically (mktemp alongside + mv), not through >
    assert "> /home/agent-ro/.ssh/authorized_keys" not in TARGET


def test_deploy_known_hosts_lookup_failure_is_friendly():
    assert 'ssh-keygen -F "$host_ip" || true' in DEPLOY
    assert "$1 !~ /^@/" in DEPLOY


def test_target_missing_exec_user_is_explicit():
    assert 'exec_passwd=$(getent passwd "$exec_user") || {' in TARGET


def test_deploy_servers_enabled_edits_are_literal_and_newline_safe():
    assert "sed -i" not in DEPLOY
    assert "grep -vxF -- " in DEPLOY
    assert '[ -n "$(tail -c1 /etc/digipaddock/servers.enabled)" ]' in DEPLOY


def test_target_check_reports_sshd_t_failure():
    assert "sshd -T failed" in TARGET


# --- review round 2 ---

def _bash_check(snippet, *args):
    """snippet reads $RE and $VAL from the environment (Git Bash on Windows mangles arguments with {n})."""
    import subprocess
    bash = _bash_exe()
    if bash is None:
        import pytest
        pytest.skip("нет bash")
    env = dict(os.environ, RE=args[0], VAL=args[1])
    return subprocess.run([bash, "-c", snippet], env=env).returncode


def test_readback_password_is_validated_before_sql():
    assert "pw_re='^Kd-[A-Za-z0-9]{28}9a$'" in TARGET
    sed_at = TARGET.index("pw=$(sed -n 's/^password=")
    check_at = TARGET.index("[[ $pw =~ $pw_re ]]", sed_at)
    first_sql = TARGET.index("IDENTIFIED BY '$pw'")
    assert sed_at < check_at < first_sql
    assert "warning: the password in .my.cnf" in TARGET
    # the final check before the SQL: the generator's format
    assert TARGET.index("the agent_ro password failed the format check") < first_sql


def test_password_regex_rejects_sql_injection_and_accepts_generator_shape():
    pw_re = re.search(r"pw_re='([^']+)'", TARGET).group(1)
    snippet = '[[ $VAL =~ $RE ]]'
    good = "Kd-" + "aB3" * 9 + "x" + "9a"
    assert len(good) == 33
    assert _bash_check(snippet, pw_re, good) == 0
    for bad in ("x'; GRANT ALL ON *.* TO 'agent_ro'@'localhost' WITH GRANT OPTION; -- ", good + "'", good + "\nx", good[:-1], "Kd-" + "a" * 28 + "9a;"):
        assert _bash_check(snippet, pw_re, bad) != 0, repr(bad)


def test_printf_formats_keep_backslash_n():
    assert "exec_home=$(printf '%s\\n' \"$exec_passwd\"" in TARGET
    assert "printf 'restrict,command=\"%s\",from=\"%s\" %s\\n'" in TARGET
    for text in (TARGET, DEPLOY):
        assert not re.search(r"printf '[^'\n]*\n[^'\n]*'", text)


def test_temp_files_are_cleaned_on_failure():
    ak = TARGET.index(".ak.XXXXXX")
    assert "trap 'rm -f \"$tmp\"' EXIT" in TARGET[ak:ak + 200]
    my = TARGET.index(".my.cnf.XXXXXX")
    assert "trap 'rm -f \"$tmp\"' EXIT" in TARGET[my:my + 200]
    assert r"trap 'rm -f \"\$t\"' EXIT" in DEPLOY[DEPLOY.index(".servers.XXXXXX") - 120:DEPLOY.index(".servers.XXXXXX") + 120]


def test_deploy_requires_ssh_port_22():
    # the agent goes to a target on the default port: the owner's alias with another port is another server or a port forward
    hostname_check = DEPLOY.index("""ssh -G "$target" | awk '$1 == "hostname" {print $2}'""")
    port_line = next(line for line in DEPLOY.splitlines() if """$1 == "port" {print $2}""" in line)
    assert hostname_check < DEPLOY.index(port_line) < DEPLOY.index('ssh "$target"')
    stub = 'ssh() { printf "hostname 198.51.100.2\\nport %s\\nuser root\\n" "$PORT"; }; target=x; '
    bash = _bash_exe()
    if bash is None:
        import pytest
        pytest.skip("нет bash")
    import subprocess
    ok = subprocess.run([bash, "-c", stub + port_line], env=dict(os.environ, PORT="22"), capture_output=True)
    assert ok.returncode == 0, ok.stderr
    bad = subprocess.run([bash, "-c", stub + port_line], env=dict(os.environ, PORT="2222"), capture_output=True,
                         encoding="utf-8", errors="replace")
    assert bad.returncode == 1 and "port 2222" in bad.stderr


def test_deploy_validates_inventory_values_before_remote_use():
    ip_re = re.search(r"ip_re='([^']+)'", DEPLOY).group(1)
    user_re = re.search(r"user_re='([^']+)'", DEPLOY).group(1)
    first_remote = DEPLOY.index('ssh "$target"')
    assert DEPLOY.index("[[ $host_ip =~ $ip_re ]]") < DEPLOY.index('ssh -G "$target"')
    assert DEPLOY.index("[[ $exec_user =~ $user_re ]]") < first_remote
    snippet = '[[ $VAL =~ $RE ]]'
    for ok in ("203.0.113.20", "0.0.0.0", "255.255.255.255"):
        assert _bash_check(snippet, ip_re, ok) == 0, ok
    for bad in ("256.1.1.1", "1.2.3", "1.2.3.4.5", "01.2.3.4", "198.51.100.2;id", "198.51.100.2\n", "a.b.c.d"):
        assert _bash_check(snippet, ip_re, bad) != 0, bad
    for ok in ("root", "ubuntu", "_a-b1"):
        assert _bash_check(snippet, user_re, ok) == 0, ok
    for bad in ("Root", "a b", "a;id", "", "a" * 33, "1abc", "x'y"):
        assert _bash_check(snippet, user_re, bad) != 0, bad


# --- the problem monitor ---

def test_monitor_service_unit_exact_lines():
    unit = (SYSADMIN / "systemd/digipaddock-monitor.service").read_text(encoding="utf-8")
    head, service = unit.split("[Service]", 1)
    assert "OnFailure=digipaddock-alert@digipaddock-monitor.service\n" in head
    assert "After=network-online.target\n" in head
    for line in ["Type=oneshot", "User=@base@", "ExecStart=/usr/local/bin/kd-monitor tick", "TimeoutStartSec=620"]:
        assert line + "\n" in service, line
    assert "\r" not in unit


def test_monitor_timer_unit_exact_lines():
    unit = (SYSADMIN / "systemd/digipaddock-monitor.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*:0/5\n" in unit and "AccuracySec=10s\n" in unit
    assert "[Install]\nWantedBy=timers.target" in unit
    assert "\r" not in unit



# --- security monitoring: the snapshot agent-sec-snapshot ---

def test_snapshot_installed_before_sudoers_checked_and_removed():
    assert "SNAPSHOT=/usr/local/bin/agent-sec-snapshot" in TARGET
    assert "--snapshot) snapshot_src=$2; shift 2 ;;" in TARGET
    assert '[ -n "$gate_src" ] && [ -n "$snapshot_src" ] && [ -n "$usage_src" ] && [ -n "$ro_key" ] && [ -n "$exec_key" ]' \
        in TARGET
    assert TARGET.index('install -o root -g root -m 0755 "$tmp" "$SNAPSHOT"') < TARGET.index('"$py" -I "$GATE" --sudoers')
    assert 'ast.parse(open(sys.argv[1], encoding="utf-8").read())\' "$snapshot_src"' in TARGET
    # a broken snapshot (the wrong Python, a copy cut short) is rejected before any change on the server
    assert TARGET.index('ast.parse(open(sys.argv[1], encoding="utf-8").read())\' "$snapshot_src"') \
        < TARGET.index("useradd --create-home")
    assert '{ echo "#!$py -I"; tail -n +2 "$snapshot_src"; } > "$tmp"' in TARGET
    assert 'rm -f /etc/sudoers.d/agent-ro "$GATE" "$SNAPSHOT" "$USAGE"' in TARGET
    assert 'SSH_ORIGINAL_COMMAND=agent-sec-snapshot "$GATE"' in TARGET
    assert '"# agent-sec-snapshot v1"*)' in TARGET
    assert 'restorecon -R /home/agent-ro/.ssh "$GATE" "$SNAPSHOT" "$USAGE"' in TARGET
    assert "for tool in sshd ufw fail2ban-client needrestart; do" in TARGET
    assert 'say "auth.log"' in TARGET


def test_gate_runs_isolated_and_sources_must_start_with_shebang():
    # -I: the gateway's sys.path[0] is /usr/local/bin (root:staff 2775 on Ubuntu), a planted shlex.py must not run as root
    assert '{ echo "#!$py -I"; tail -n +2 "$gate_src"; } > "$tmp"' in TARGET
    assert '{ echo "#!$py"; tail' not in TARGET
    assert '"$py" -I "$GATE" --sudoers > "$tmp"' in TARGET
    assert '"$py" "$GATE"' not in TARGET
    # without "#!" in the first line tail -n +2 would drop a line of code — the check comes before either file is installed
    check = 'for src in "$gate_src" "$snapshot_src" "$usage_src" "$exec_gate_src"; do\n  [ "$(head -c2 "$src")" = \'#!\' ] || {'
    assert check in TARGET
    assert TARGET.index(check) < TARGET.index('tail -n +2 "$gate_src"') < TARGET.index('tail -n +2 "$snapshot_src"') \
        < TARGET.index('tail -n +2 "$usage_src"')


def test_agent_ro_password_star_only_when_needed():
    assert """if [ "$(getent shadow agent-ro | cut -d: -f2)" != '*' ]; then usermod -p '*' agent-ro; fi""" in TARGET
    assert TARGET.count("usermod -p '*' agent-ro") == 1


def test_deploy_puts_snapshot_and_passes_it():
    assert "put agents/sysadmin/gate/agent_sec_snapshot.py agent_sec_snapshot.py" in DEPLOY
    assert "--snapshot $remote_dir/agent_sec_snapshot.py" in DEPLOY
    assert DEPLOY.index("put agents/sysadmin/gate/agent_sec_snapshot.py") < DEPLOY.index("target_setup.sh --gate")


# --- Hindsight's usage: agent-hindsight-usage ---

def test_hindsight_usage_installed_before_sudoers_checked_and_removed():
    assert "USAGE=/usr/local/bin/agent-hindsight-usage" in TARGET
    assert "--usage) usage_src=$2; shift 2 ;;" in TARGET
    parsed = 'ast.parse(open(sys.argv[1], encoding="utf-8").read())\' "$usage_src"'
    assert TARGET.index(parsed) < TARGET.index("useradd --create-home")
    # the copy counts the days of the factory host's zone (host.json timezone); sudoers pins this copy
    assert ('{ echo "#!$py -I"; tail -n +2 "$usage_src" | sed "s|^ZONE = \\"UTC\\"  #|ZONE = \\"$timezone_name\\"  #|"; } > "$tmp"'
            in TARGET)
    assert 'grep -c "^ZONE = \\"$timezone_name\\"  #" "$tmp"' in TARGET
    assert "--timezone) timezone_name=$2; shift 2 ;;" in TARGET
    assert TARGET.index('install -o root -g root -m 0755 "$tmp" "$USAGE"') < TARGET.index('"$py" -I "$GATE" --sudoers')
    assert 'SSH_ORIGINAL_COMMAND=agent-hindsight-usage "$GATE"' in TARGET
    assert '"# agent-hindsight-usage v1"*)' in TARGET


def test_deploy_puts_hindsight_usage_and_passes_it():
    assert "put agents/sysadmin/gate/agent_hindsight_usage.py agent_hindsight_usage.py" in DEPLOY
    assert "--usage $remote_dir/agent_hindsight_usage.py" in DEPLOY
    assert "--timezone $TIMEZONE" in DEPLOY and "TIMEZONE=$(host_value timezone)" in DEPLOY
    assert DEPLOY.index("put agents/sysadmin/gate/agent_hindsight_usage.py") < DEPLOY.index("target_setup.sh --gate")


def test_the_deployments_gate_list_comes_from_the_overlay_checked_before_it_lands():
    """Review 04.11.2026: the gate's public list holds no deployment's paths; deploy_target.sh ships the overlay's
    host/gate_forbidden.json (sha256-checked) and target_setup.sh installs it root's, 0644, after the gate's checks."""
    assert "HEAD:host/gate_forbidden.json" in DEPLOY and 'forbidden_args="--forbidden $remote_dir/gate_forbidden.json"' in DEPLOY
    assert "$forbidden_args --exec-user" in DEPLOY
    assert "--forbidden) forbidden_src=$2; shift 2 ;;" in TARGET
    assert 'install -o root -g root -m 0644 "$forbidden_src" "$FORBIDDEN"' in TARGET
    assert TARGET.index("re.compile(p, re.ASCII)") < TARGET.index('"$forbidden_src" "$FORBIDDEN"')
    assert "FORBIDDEN=$FORBIDDEN_DIR/forbidden.json" in TARGET and "/etc/agent-ro-gate" in TARGET
    example = json.loads((SYSADMIN.parents[1] / "host" / "gate_forbidden.example.json").read_text(encoding="utf-8"))
    assert example["schema_version"] == 1 and all(re.compile(p) for p in example["forbidden"])


# --- the exec gate, the owner's signer registry and the observe|enforce mode on every target ---

def test_target_setup_installs_the_exec_gate_and_forced_command():
    assert "EXEC_GATE=/usr/local/bin/agent-exec-gate" in TARGET
    for flag in ("--exec-gate) exec_gate_src=$2", "--exec-signers) exec_signers_src=$2", "--exec-mode) exec_mode=$2",
                 "--alias) alias=$2", "--rp-id) rp_id=$2", "--origin) origin=$2"):
        assert flag in TARGET
    # the exec key's line is the gate's forced command; the guard compares the whole line, not only the key
    assert 'exec_line="restrict,command=\\"$EXEC_GATE\\",from=\\"$STAGE_IP\\" $exec_key"' in TARGET
    assert 'if ! grep -qxF -- "$exec_line" "$exec_auth" \\' in TARGET
    assert 'edit_keys "$exec_auth" "$exec_line" "$EXEC_COMMENT" "$exec_blob"' in TARGET
    assert 'restrict,from=\\"$STAGE_IP\\" $exec_key' not in TARGET
    # the gate is a python source like the ro gate: ast.parse, a shebang, -I
    assert 'for src in "$gate_src" "$snapshot_src" "$usage_src" "$exec_gate_src"; do' in TARGET
    assert '{ echo "#!$py -I"; tail -n +2 "$exec_gate_src"; } > "$tmp"' in TARGET
    assert 'install -o root -g root -m 0755 "$tmp" "$EXEC_GATE"' in TARGET
    # settings and registry are root's, 0644; the registry is read by the gate's own reader before it lands
    assert "EXEC_GATE_JSON=$EXEC_GATE_DIR/gate.json" in TARGET and "EXEC_GATE_DIR=/etc/agent-exec-gate" in TARGET
    assert "EXEC_SIGNERS=$EXEC_GATE_DIR/signers.json" in TARGET and "EXEC_USED=/var/lib/agent-exec-gate/used" in TARGET
    assert '"$py" -I "$EXEC_GATE" --check-signers "$exec_signers_src"' in TARGET
    assert TARGET.index('--check-signers "$exec_signers_src"') < TARGET.index('"$exec_signers_src" "$EXEC_SIGNERS"')
    assert 'install -o root -g root -m 0644 "$exec_signers_src" "$EXEC_SIGNERS"' in TARGET
    assert 'install -o root -g root -m 0644 "$tmp" "$EXEC_GATE_JSON"' in TARGET
    assert 'install -d -o "$exec_user" -g "$(id -gn "$exec_user")" -m 0700 "$EXEC_USED"' in TARGET
    # the values of gate.json are checked before anything is written; the mode stays unless given, observe at first
    assert "[[ $alias =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]" in TARGET
    assert "[[ $rp_id =~ ^[a-z0-9.-]+$ ]]" in TARGET and "[[ $origin =~ ^https://[a-z0-9.-]+$ ]]" in TARGET
    assert "[[ -z $exec_mode || $exec_mode =~ ^(observe|enforce)$ ]]" in TARGET
    assert 'print(m if m in ("observe", "enforce") else "observe")' in TARGET
    # self-check: an empty envelope is refused in both modes (126, "refusal"), then the gate reports its mode
    assert "SSH_ORIGINAL_COMMAND='agent-exec-gate run' \"$EXEC_GATE\" </dev/null" in TARGET
    assert "126:*отказ*) ;;" in TARGET
    assert '"$py" -I "$EXEC_GATE" --check' in TARGET
    # --check names openssl as a blocker; --remove deletes the gate, its settings, the registry and the used dir
    check = TARGET.split('if [ "$mode" = check ]')[1].split("fi")[0]
    assert "openssl" in check
    remove = TARGET.split('if [ "$mode" = remove ]')[1].split("exit 0")[0]
    for path in ('"$EXEC_GATE"', '"$EXEC_GATE_JSON"', '"$EXEC_SIGNERS"', 'rm -rf "$EXEC_USED"'):
        assert path in remove


def test_target_setup_keeps_the_first_signers_unless_told_to_replace():
    # review I1: the registry comes from the factory host — once a server trusts a set of keys and a cabinet,
    # a different one is refused (old -> new printed) unless the owner's machine says --replace-signers
    assert "--replace-signers) replace_signers=1; shift ;;" in TARGET
    section = TARGET.split("# 2d. Exec gate")[1].split("# 3. sudoers")[0]
    assert 'if [ -f "$EXEC_SIGNERS" ]; then' in section
    assert '"$EXEC_SIGNERS" "$exec_signers_src" "$rp_id" "$origin"' in section  # old, candidate (or none), cabinet
    assert 'if keys(old) != keys(new):' in section and 'if (old.get("rp_id"), old.get("origin")) != (rp_id, origin):' in section
    assert '[ -n "$replace_signers" ] || { echo "ERROR: the signing keys or the cabinet this server trusts would change' in section
    assert "run again with --replace-signers" in section
    assert 'echo "replacing the signers registry of $(hostname) (--replace-signers)"' in section
    # the comparison runs before anything is replaced, after the candidate passed the gate's reader
    assert section.index('--check-signers "$exec_signers_src"') < section.index('if keys(old) != keys(new):') \
        < section.index('install -o root -g root -m 0644 "$exec_signers_src" "$EXEC_SIGNERS"')
    # the diff is written as utf-8 bytes: a Python 3.6 target with the C locale cannot print a non-ASCII key name
    assert "sys.stderr.buffer.write" in section


def test_deploy_target_passes_replace_signers_through():
    assert "--replace-signers) replace_signers=1; shift ;;" in DEPLOY
    assert '--check|--remove) [ $# -eq 1 ] && [ -z "$mysql_args$exec_mode$replace_signers" ]' in DEPLOY
    assert "${exec_mode:+ --exec-mode $exec_mode}${replace_signers:+ --replace-signers}\"" in DEPLOY
    assert "[--exec-mode observe|enforce] [--replace-signers]" in DEPLOY


def test_deploy_target_fetches_signers_and_passes_the_mode():
    # the registry comes from the cabinet on the factory host (root), is checked locally, then goes with the gate
    assert "cmd kd-cabinet) user signers" in DEPLOY
    assert "kd-cabinet user signing <id> --credential <prefix> on" in DEPLOY
    for check in ('d.get("schema_version") == 1', 're.match(r"^[a-z0-9.-]+$", d["rp_id"])',
                  're.match(r"^https://[a-z0-9.-]+$", d["origin"])', "1 <= len(s) <= 20",
                  're.match(r"^[a-z][a-z0-9_-]{0,31}$", e["user"])', 'for k in ("credential_id", "public_key"):'):
        assert check in DEPLOY
    assert 'print("signers: %s (%s, %s…)"' in DEPLOY
    assert "put agents/sysadmin/gate/agent_exec_gate.py agent_exec_gate.py" in DEPLOY
    assert "sha256 of exec_signers.json does not match" in DEPLOY
    assert ('exec_args="--exec-gate $remote_dir/agent_exec_gate.py --exec-policy $remote_dir/exec_policy.py '
            '$units_args --exec-signers $remote_dir/exec_signers.json '
            '--alias $target --rp-id $rp_id --origin $origin${exec_mode:+ --exec-mode $exec_mode}' in DEPLOY)
    assert "--timezone $TIMEZONE $exec_args" in DEPLOY
    # --exec-mode alone or with --mysql-db; --check and --remove take nothing else
    assert "--exec-mode) [[ ${2:-} =~ ^(observe|enforce)$ ]]" in DEPLOY
    assert '--check|--remove) [ $# -eq 1 ] && [ -z "$mysql_args$exec_mode$replace_signers" ]' in DEPLOY
    # the signers are fetched and checked before the first command on the target
    assert DEPLOY.index("cmd kd-cabinet) user signers") < DEPLOY.index('ssh "$target"')
    assert DEPLOY.index("put agents/sysadmin/gate/agent_exec_gate.py") < DEPLOY.index("target_setup.sh --gate")


# --- the gate's policy as the second file, the site units into gate.json ---

def test_target_setup_installs_the_policy_next_to_the_gate():
    assert "EXEC_POLICY=/usr/local/bin/agent-exec-policy" in TARGET
    assert "--exec-policy) exec_policy_src=$2; shift 2 ;;" in TARGET
    assert '[ -n "$exec_policy_src" ] || { echo "--exec-policy <file> is required' in TARGET
    assert "ast.parse(open(sys.argv[1], encoding=\"utf-8\").read())' \"$exec_policy_src\"" in TARGET
    # a module, not a command: 0644, root's, and it lands before the gate that needs it
    section = TARGET.split("# 2d. Exec gate")[1].split("# 3. sudoers")[0]
    assert 'install -o root -g root -m 0644 "$exec_policy_src" "$EXEC_POLICY"' in section
    assert section.index('"$EXEC_POLICY"') < section.index('install -o root -g root -m 0755 "$tmp" "$EXEC_GATE"')
    # --check reports it, --remove deletes it, restorecon labels it
    check = TARGET.split('if [ "$mode" = check ]')[1].split("\nfi\n")[0]
    assert 'say "exec policy"' in check and '"$EXEC_POLICY"' in check
    remove = TARGET.split('if [ "$mode" = remove ]')[1].split("exit 0")[0]
    assert '"$EXEC_POLICY"' in remove
    assert '"$EXEC_GATE" "$EXEC_POLICY" "$EXEC_GATE_DIR"' in TARGET


def test_target_setup_writes_the_site_units_into_gate_json():
    assert "--critical-units) critical_units=$2; critical_units_given=1; shift 2 ;;" in TARGET
    assert ("[[ -z $critical_units || $critical_units =~ ^[A-Za-z0-9*][A-Za-z0-9@._*-]{0,120}"
            "( [A-Za-z0-9*][A-Za-z0-9@._*-]{0,120})*$ ]]") in TARGET
    snippet = TARGET.split("\"$py\" -I -c 'import json, sys\ngate = ")[1].split("' \\\n")[0]
    code = "import json, sys\ngate = " + snippet
    for given, units, expected in (("", "", None), ("1", "", []), ("1", "app-* *.example.com*", ["app-*", "*.example.com*"])):
        proc = subprocess.run([sys.executable, "-I", "-c", code, "node-c", "cabinet.example.test",
                               "https://cabinet.example.test", "observe", given, units],
                              stdout=subprocess.PIPE, universal_newlines=True, check=True)
        assert json.loads(proc.stdout).get("critical_units") == expected


def test_target_setup_self_check_probes_rule1_through_the_gate(tmp_path, monkeypatch):
    # the probe envelope of the self-check is unsigned: in observe only the policy keeps it from running
    from gate import agent_exec_gate as gate

    probe = TARGET.split("probe='")[1].split("'\n")[0]
    assert "echo x > /etc/sudoers.d/x" in json.loads(probe)["command"]
    assert "126:*deploy_target.sh*) echo \"exec policy: rule 1 refuses a write into sudoers.d\" ;;" in TARGET
    policy = Path(gate.__file__).resolve().parents[3] / "common" / "kd" / "exec_policy.py"
    monkeypatch.setattr(gate, "POLICY_PATH", str(policy))
    monkeypatch.setattr(gate, "_POLICY", {})
    monkeypatch.setattr(gate, "_log", lambda message, level=None: None)
    ran = []
    monkeypatch.setattr(gate, "run", lambda command, as_root, timeout=gate.TIMEOUT: ran.append(command) or 0)
    for mode in ("observe", "enforce"):
        settings = tmp_path / "gate.json"
        settings.write_text(json.dumps({"alias": "node-c", "rp_id": "cabinet.example.test",
                                        "origin": "https://cabinet.example.test", "mode": mode}), encoding="utf-8")
        monkeypatch.setattr(gate, "GATE_JSON", str(settings))
        err = io.BytesIO()
        code = gate.main([], environ={"SSH_ORIGINAL_COMMAND": "agent-exec-gate run"},
                         stdin=io.BytesIO(probe.encode("utf-8")), out=io.BytesIO(), err=err)
        assert code == 126 and "deploy_target.sh" in err.getvalue().decode("utf-8")
    assert ran == []


def test_installers_parse_as_bash():
    for name in ("target_setup.sh", "deploy_target.sh"):
        proc = subprocess.run(["bash", "-n", str(SYSADMIN / "install" / name)], stderr=subprocess.PIPE,
                              universal_newlines=True)
        assert proc.returncode == 0, proc.stderr


def test_deploy_target_sends_the_policy_and_the_overlays_site_units():
    assert "put common/kd/exec_policy.py exec_policy.py" in DEPLOY
    assert DEPLOY.index("put common/kd/exec_policy.py") < DEPLOY.index("target_setup.sh --gate")
    assert 'git -C "$DEPLOY_TREE" show HEAD:agents/sysadmin/operations.json' in DEPLOY
    assert "u = d.get('critical_units')" in DEPLOY
    assert "units_args=\"--critical-units '$units'\"" in DEPLOY
    # checked locally before it reaches a remote command: unit patterns only, no quote can leave the quotes
    assert ("[[ -z $units || $units =~ ^[A-Za-z0-9*][A-Za-z0-9@._*-]{0,120}"
            "( [A-Za-z0-9*][A-Za-z0-9@._*-]{0,120})*$ ]]") in DEPLOY



def test_target_setup_installs_the_app_flag_helper_only_when_it_is_given():
    """agent-app-flag is what an app.flag plan step runs as the application's account. It lives outside
    /usr/local/bin/agent-* (rule 1 of the exec policy refuses a plan that names those) and is optional: an install
    without --app-flag requires nothing new and leaves an installed helper alone."""
    assert "APP_FLAG=/usr/local/libexec/agent-app-flag" in TARGET
    assert "--app-flag) app_flag_src=$2; shift 2 ;;" in TARGET
    assert "--app-flag <file> is required" not in TARGET
    section = TARGET.split("# 2e. App flag helper")[1].split("# 3. sudoers")[0]
    assert 'if [ -n "$app_flag_src" ]; then' in section
    # a python source checked like the gates; root's, read and run by any account, never writable by the application
    assert "ast.parse(open(sys.argv[1], encoding=\"utf-8\").read())' \"$app_flag_src\"" in section
    assert 'install -d -o root -g root -m 0755 "$(dirname "$APP_FLAG")"' in section
    assert 'install -o root -g root -m 0755 "$app_flag_src" "$APP_FLAG"' in section
    # --check reports it, --remove deletes it, restorecon labels it
    check = TARGET.split('if [ "$mode" = check ]')[1].split("\nfi\n")[0]
    assert 'say "app flag helper"' in check and '"$APP_FLAG"' in check
    remove = TARGET.split('if [ "$mode" = remove ]')[1].split("exit 0")[0]
    assert '"$APP_FLAG"' in remove
    assert 'restorecon "$APP_FLAG" || true' in section


def test_the_typed_op_helper_is_installed_checked_and_removed_like_the_flag_helper():
    """cron.line, file.drop and gate.script run /usr/local/libexec/agent-typed-op as root."""
    assert "TYPED_OP=/usr/local/libexec/agent-typed-op" in TARGET
    assert 'install -o root -g root -m 0755 "$typed_op_src" "$TYPED_OP"' in TARGET
    check = TARGET.split('if [ "$mode" = check ]')[1].split("\nfi\n")[0]
    assert 'say "typed op helper"' in check and 'say "script steps"' in check
    remove = TARGET.split('if [ "$mode" = remove ]')[1].split("exit 0")[0]
    assert '"$TYPED_OP"' in remove and '"$EXEC_GATE_DIR/script-off"' in remove
    assert "put agents/sysadmin/gate/agent_typed_op.py agent_typed_op.py" in DEPLOY
    assert "--typed-op $remote_dir/agent_typed_op.py" in DEPLOY


def test_deploy_puts_the_app_flag_helper_and_passes_it():
    assert "put agents/sysadmin/gate/agent_app_flag.py agent_app_flag.py" in DEPLOY
    assert "--app-flag $remote_dir/agent_app_flag.py" in DEPLOY
    assert DEPLOY.index("put agents/sysadmin/gate/agent_app_flag.py") < DEPLOY.index("target_setup.sh --gate")


def test_target_setup_installs_the_list_of_test_environments():
    """the exec gate's own list of test environments — root's file next to gate.json, checked by the gate
    before it lands; a list with no entries removes the installed one (the switch back to «everything is signed»)."""
    assert "TEST_ENVS=$EXEC_GATE_DIR/test-envs.json" in TARGET
    assert "--test-envs) test_envs_src=$2; shift 2 ;;" in TARGET
    section = TARGET.split("# 2f. Test environments")[1].split("# 3. sudoers")[0]
    assert 'if [ -n "$test_envs_src" ]; then' in section
    assert '"$py" -I "$EXEC_GATE" --check-test-envs "$test_envs_src"' in section
    assert 'install -o root -g root -m 0644 "$test_envs_src" "$TEST_ENVS"' in section
    assert 'rm -f "$TEST_ENVS"' in section
    assert section.index("--check-test-envs") < section.index("install -o root")
    # the gate is installed before it checks the list
    assert TARGET.index('install -o root -g root -m 0755 "$tmp" "$EXEC_GATE"') < TARGET.index("# 2f. Test environments")
    check = TARGET.split('if [ "$mode" = check ]')[1].split("\nfi\n")[0]
    assert 'say "test environments"' in check and '"$TEST_ENVS"' in check
    remove = TARGET.split('if [ "$mode" = remove ]')[1].split("exit 0")[0]
    assert '"$TEST_ENVS"' in remove


def test_deploy_builds_the_list_from_the_overlays_catalog_and_never_for_production():
    assert ('git -C "$DEPLOY_TREE" show HEAD:agents/sysadmin/operations.json | ${PYTHON:-python} -I '
            'agents/sysadmin/gate/agent_exec_gate.py --test-envs "$target"') in DEPLOY
    assert "PROD=$(host_value prod_alias)" in DEPLOY
    block = _envs_block()
    assert '[ "$target" = "$PROD" ]' in block and "exit 1" in block.split('[ "$target" = "$PROD" ]')[1]
    assert "sha256sum $remote_dir/test_envs.json" in block
    assert "--test-envs $remote_dir/test_envs.json" in DEPLOY
    assert DEPLOY.index('envs_args="--test-envs $remote_dir/test_envs.json"') < DEPLOY.index("target_setup.sh --gate")
    assert "test environments of $target:" in block  # the owner sees what runs unsigned there


def _envs_block():
    start = DEPLOY.index("envs='")
    end = DEPLOY.index('"$signers" | ssh', start)
    return DEPLOY[start:end]


def test_deploy_installs_only_a_list_of_a_catalog_the_cabinet_loads():
    """The gate's reader of the catalog is looser than the cabinet's — the catalog's own rules
    (application roots, accounts, units of the server, no test environment on production) are checked first."""
    block = _envs_block()
    assert "from kd import admin_operations as ops" in block
    assert "ops.Catalog.from_dict(" in block and "protected=(sys.argv[1],)" in block and '" "$PROD"' in block
    assert block.index("ops.Catalog.from_dict(") < block.index('--test-envs "$target"')
    assert "is not a catalog the cabinet loads" in block


def test_an_overlay_without_a_catalog_takes_the_servers_list_away():
    """No catalog in the overlay means no test environments — the empty list goes to the server,
    and target_setup.sh removes an installed one."""
    block = _envs_block()
    assert block.index("envs='{\"schema_version\": 1, \"envs\": {}}'") < block.index("cat-file -e")
    after = block.split("\nfi\n")[-1]  # the transfer is outside the «catalog exists» branch
    assert "sha256sum $remote_dir/test_envs.json" in after
    assert 'envs_args="--test-envs $remote_dir/test_envs.json"' in after


def test_target_setup_opens_the_test_environments_reads():
    """the directories a test environment's entry lists under read are opened to agent-ro by an ACL (and a
    default one for files written later); the installed list's reads close first, and close with the removal."""
    acl = TARGET.split("test_env_acl() {")[1].split("\n}\n")[0]
    assert "setfacl -R -P -m u:agent-ro:rX,d:u:agent-ro:rX" in acl
    assert "setfacl -R -P -x u:agent-ro,d:u:agent-ro" in acl
    assert '[ -L "$path" ]' in acl and 'readlink -f "$path"' in acl  # a link is never followed into the grant
    assert '! -type d -links +1' in acl  # nor a hard link: the ACL belongs to the inode
    section = TARGET.split("# 2f. Test environments")[1].split("# 3. sudoers")[0]
    assert section.index("test_env_acl revoke $old_reads") < section.index("test_env_acl grant $new_reads")
    assert section.index('install -o root -g root -m 0644 "$test_envs_src"') < section.index("test_env_acl grant")
    assert '--test-env-reads "$test_envs_src"' in section
    remove = TARGET.split('if [ "$mode" = remove ]')[1].split("exit 0")[0]
    assert remove.index("test_env_acl revoke") < remove.index('"$EXEC_GATE"')
    check = TARGET.split('if [ "$mode" = check ]')[1].split("\nfi\n")[0]
    assert 'say "test env read"' in check
