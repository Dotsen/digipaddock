#!/usr/bin/env bash
# A target server of the sysadmin: agent-ro + the gateway + sudoers + the exec key (+ MySQL agent_ro).
# Idempotent, as root. deploy_target.sh runs it from the owner's machine — as a file, not through stdin.
# usage:
#   target_setup.sh --check [--exec-user <user>] --stage-ip <ip> --stage-alias <alias>     — a read-only precheck; exit 1 — there are blockers
#   target_setup.sh --gate <file> --snapshot <file> --usage <file> [--forbidden <file>] --exec-gate <file> --exec-policy <file> [--app-flag <file>] [--typed-op <file>] [--critical-units '<pattern> …'] [--exec-signers <file>] [--exec-mode observe|enforce] [--replace-signers] --alias <this server's alias> --rp-id <cabinet host> --origin https://<cabinet host> --ro-key '<pub>' --exec-key '<pub>' [--exec-user <user>] [--mysql-db <database> --mysql-tables '<table> …'] [--data-key '<pub>' --data-dbs '<database> …' [--data-hidden '<database>.<table>.<column> …']] --stage-ip <ip> --stage-alias <alias> [--timezone <IANA zone>]
#   target_setup.sh --remove [--exec-user <user>] [--exec-key '<pub>'] --stage-ip <ip> --stage-alias <alias>   — with --exec-key the key's lines are removed by its blob too
# deploy_target.sh passes the factory host (address and alias) from the deployment's host.json.
set -euo pipefail
GATE=/usr/local/bin/agent-ro-gate
SNAPSHOT=/usr/local/bin/agent-sec-snapshot
USAGE=/usr/local/bin/agent-hindsight-usage  # Hindsight usage (prints "none" on targets without Hindsight)
# the deployment's own secret paths for the gate (host/gate_forbidden.json of the overlay); without --forbidden an
# installed list stays as it is
FORBIDDEN_DIR=/etc/agent-ro-gate
FORBIDDEN=$FORBIDDEN_DIR/forbidden.json
# the exec key's forced command — a step runs only as an envelope the owner signed (enforce) or is logged
# (observe); gate.json holds this server's alias, the cabinet's rp_id and origin and the mode, signers.json the
# owner's signing passkeys (kd-cabinet user signers); used/ records every critical action run once
EXEC_GATE=/usr/local/bin/agent-exec-gate
EXEC_GATE_DIR=/etc/agent-exec-gate
EXEC_GATE_JSON=$EXEC_GATE_DIR/gate.json
EXEC_SIGNERS=$EXEC_GATE_DIR/signers.json
EXEC_USED=/var/lib/agent-exec-gate/used
# the gate's policy (kd.exec_policy) — a module the gate compiles from this file, not a command; the site
# units it keeps critical to stop go into gate.json (critical_units; without them every unit's stop is critical)
EXEC_POLICY=/usr/local/bin/agent-exec-policy
# what an app.flag plan step runs as the application's account. Not /usr/local/bin/agent-*: rule 1 of
# the exec policy refuses a plan that names those, and this one a signed plan does name
APP_FLAG=/usr/local/libexec/agent-app-flag
# what a cron.line, file.drop or gate.script plan step runs as root (the same reason for libexec)
TYPED_OP=/usr/local/libexec/agent-typed-op
# this server's own list of test environments — the steps the exec gate runs without the owner's signature
TEST_ENVS=$EXEC_GATE_DIR/test-envs.json

gate_src="" snapshot_src="" usage_src="" forbidden_src="" ro_key="" exec_key="" exec_user=root mysql_db="" mysql_tables="" mode=install
data_key="" data_dbs="" data_hidden=""
exec_gate_src="" exec_signers_src="" exec_mode="" replace_signers="" alias="" rp_id="" origin=""
exec_policy_src="" critical_units="" critical_units_given="" app_flag_src="" test_envs_src="" typed_op_src=""
stage_ip="" stage_alias="" timezone_name=UTC
while [ $# -gt 0 ]; do
  case $1 in
    --check) mode=check; shift ;;
    --remove) mode=remove; shift ;;
    --gate) gate_src=$2; shift 2 ;;
    --snapshot) snapshot_src=$2; shift 2 ;;
    --usage) usage_src=$2; shift 2 ;;
    --forbidden) forbidden_src=$2; shift 2 ;;
    --exec-gate) exec_gate_src=$2; shift 2 ;;
    --exec-policy) exec_policy_src=$2; shift 2 ;;
    --app-flag) app_flag_src=$2; shift 2 ;;
    --typed-op) typed_op_src=$2; shift 2 ;;
    --test-envs) test_envs_src=$2; shift 2 ;;
    --critical-units) critical_units=$2; critical_units_given=1; shift 2 ;;
    --exec-signers) exec_signers_src=$2; shift 2 ;;
    --exec-mode) exec_mode=$2; shift 2 ;;
    --replace-signers) replace_signers=1; shift ;;
    --alias) alias=$2; shift 2 ;;
    --rp-id) rp_id=$2; shift 2 ;;
    --origin) origin=$2; shift 2 ;;
    --ro-key) ro_key=$2; shift 2 ;;
    --exec-key) exec_key=$2; shift 2 ;;
    --exec-user) exec_user=$2; shift 2 ;;
    --mysql-db) mysql_db=$2; shift 2 ;;
    --mysql-tables) mysql_tables=$2; shift 2 ;;
    --data-key) data_key=$2; shift 2 ;;
    --data-dbs) data_dbs=$2; shift 2 ;;
    --data-hidden) data_hidden=$2; shift 2 ;;
    --stage-ip) stage_ip=$2; shift 2 ;;
    --stage-alias) stage_alias=$2; shift 2 ;;
    --timezone) timezone_name=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ $stage_ip =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "--stage-ip <IPv4 of the factory host> is required" >&2; exit 2; }
[[ $stage_alias =~ ^[A-Za-z0-9][A-Za-z0-9-]*$ ]] || { echo "--stage-alias <alias of the factory host> is required" >&2; exit 2; }
# the factory host's time zone (host.json timezone): a day of agent-hindsight-usage is counted in it, as in kd-usage
[[ $timezone_name =~ ^(UTC|[A-Z][A-Za-z_]+(/[A-Za-z0-9_+-]+){1,2})$ ]] || { echo "--timezone <IANA zone>: $timezone_name" >&2; exit 2; }
STAGE_IP=$stage_ip
RO_COMMENT=agent-ro@$stage_alias
EXEC_COMMENT=agent-exec@$stage_alias
DATA_COMMENT=agent-data@$stage_alias  # kd-data's key, forced into agent-ro-gate --data
DATA_CNF=/home/agent-ro/.my-data.cnf
# a column whose name says it holds a secret is not granted to agent_data (passwords and their hashes, API
# keys, tokens, cookies, salts, one-time codes); --data-hidden names the deployment's others. MySQL REGEXP, any case.
DATA_SECRET_RE='password|passwd|pwd|secret|token|cookie|api_?key|auth_?key|priv(ate)?_?key|credential|validator|salt|(^|_)pass(_|$)|(^|_)t?otp(_|$)'
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
exec_passwd=$(getent passwd "$exec_user") || { echo "no user $exec_user" >&2; exit 1; }
exec_home=$(printf '%s\n' "$exec_passwd" | cut -d: -f6)
[ -n "$exec_home" ] || { echo "no user $exec_user" >&2; exit 1; }
exec_auth=$exec_home/.ssh/authorized_keys
mysql_root() { mysql --defaults-file=/etc/mysql/debian.cnf "$@"; }

find_python() {
  local candidate
  for candidate in /usr/bin/python3 /usr/libexec/platform-python; do
    if [ -x "$candidate" ] && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 6))'; then
      echo "$candidate"; return 0
    fi
  done
  return 1
}

# The home of agent-ro belongs to root, but having bypassed the gateway agent-ro could have left a symlink in it:
# a write through it (install -d, mv, rm) would hit a foreign file, /root/.ssh/authorized_keys for one. A symlink
# is refused.
refuse_symlinks() {
  local p
  for p in /home/agent-ro/.ssh /home/agent-ro/.ssh/authorized_keys /home/agent-ro/.my.cnf "$DATA_CNF"; do
    if [ -L "$p" ]; then
      echo "ERROR: $p is a symbolic link (agent-ro may have planted it); sort it out by hand on $(hostname), nothing was touched" >&2
      exit 1
    fi
  done
}

# --- test_env_reads
# A test environment's directories its catalog entry lists under "read" (its journals) are opened to agent-ro whole,
# whatever the application's own file modes: an ACL entry on the tree, a default one for what the application writes
# there later, and traverse on the directories above it up to the applications' root (/var/www, /opt, /srv). The
# read-only gate's own path rules (config.ini, .env, keys) still apply to everything agent-ro reads there.
test_env_reads() {  # test_env_reads <list file>: the directories, one per line; nothing for a missing file
  [ -f "$1" ] || return 0
  "$(find_python)" -I "$EXEC_GATE" --test-env-reads "$1"
}

test_env_acl() {  # test_env_acl grant|revoke <directory>...
  local action=$1 path up
  shift
  for path in "$@"; do
    if [ "$action" = grant ]; then
      if [ -L "$path" ] || [ ! -d "$path" ] || [ "$(readlink -f "$path")" != "$path" ]; then
        echo "warning: test environment read $path is not a plain directory: not opened" >&2
        continue
      fi
      # an ACL belongs to the inode: a hard link planted there would open a file of another name (config.ini)
      if [ -n "$(find "$path" ! -type d -links +1 -print -quit)" ]; then
        echo "warning: test environment read $path holds a hard-linked file: not opened" >&2
        continue
      fi
      command -v setfacl >/dev/null \
        || { echo "ERROR: setfacl is missing (package acl): test environment reads cannot be opened" >&2; exit 1; }
      setfacl -R -P -m u:agent-ro:rX,d:u:agent-ro:rX "$path"
    elif [ -d "$path" ] && [ ! -L "$path" ]; then
      setfacl -R -P -x u:agent-ro,d:u:agent-ro "$path" 2>/dev/null || true
    fi
    up=$(dirname "$path")
    while case $up in /|/var/www|/opt|/srv) false ;; *) true ;; esac; do
      if [ "$action" = grant ]; then setfacl -m u:agent-ro:x "$up"; else setfacl -x u:agent-ro "$up" 2>/dev/null || true; fi
      up=$(dirname "$up")
    done
  done
}

# --- edit_keys
# Editing authorized_keys without the risk of losing the login: a copy beside it (on a full disk it fails before
# the edit), the new file is assembled beside it and swapped in by mv; a grep failure (code > 1) and a wrong line
# count stop it. There are several substrings — the exec key is removed both by its comment and by the key's blob
# (a line without the comment or with another one would let the same key past the gateway).
edit_keys() {  # edit_keys <файл> <добавить строку или ""> <убрать строки с подстрокой>...
  local file=$1 add=$2 dir tmp rc before removed expected after needle
  local -a patterns=()
  shift 2
  for needle in "$@"; do
    if [ -n "$needle" ]; then patterns+=(-e "$needle"); fi
  done
  [ "${#patterns[@]}" -gt 0 ] || { echo "edit_keys $file: nothing to remove by" >&2; exit 1; }
  dir=$(dirname "$file")
  cp -a "$file" "$file.bak-kd-$(date +%Y%m%d%H%M%S)"
  tmp=$(mktemp "$dir/.authorized_keys.XXXXXX")
  rc=0
  grep -vF "${patterns[@]}" "$file" > "$tmp" || rc=$?
  [ "$rc" -le 1 ] || { rm -f "$tmp"; echo "grep $file: exit code $rc, the file is untouched" >&2; exit 1; }
  if [ -n "$add" ]; then printf '%s\n' "$add" >> "$tmp"; fi
  before=$(grep -c '' "$file" || true)
  removed=$(grep -cF "${patterns[@]}" "$file" || true)
  expected=$((before - removed))
  if [ -n "$add" ]; then expected=$((expected + 1)); fi
  after=$(grep -c '' "$tmp" || true)
  [ "$after" -eq "$expected" ] || { rm -f "$tmp"; echo "$file: expected $expected lines, got $after; untouched" >&2; exit 1; }
  # run by the exec user itself (exec_edit_keys) it cannot hand the file to anyone: the owner stays, a group it is not in too
  chown --reference="$file" "$tmp" || [ "$(id -u)" != 0 ]
  chmod --reference="$file" "$tmp"
  mv -f "$tmp" "$file"
  if command -v restorecon >/dev/null; then restorecon "$file" || true; fi
}

key_blob() {  # key_blob '<type> <blob> <comment>' — the key's base64 blob, "" for an empty or malformed key
  local type blob rest
  read -r type blob rest <<< "${1:-}" || true
  if [[ ${blob:-} =~ ^[A-Za-z0-9+/]{40,}={0,2}$ ]]; then printf '%s\n' "$blob"; fi
}
# The exec user's ~/.ssh belongs to that user, who can replace it or authorized_keys with a symlink: root following
# it would write (install, cp, mv, chown) into a system directory. So for any exec user but root the edit is made by
# the exec user itself (the same function, in its own shell); root's own ~/.ssh is root's.
exec_edit_keys() {  # the arguments of edit_keys
  if [ "$exec_user" = root ]; then
    edit_keys "$@"
  else
    # the same shell options as this script: a failed copy (a full disk) stops the edit before the file is touched
    runuser -u "$exec_user" -- bash -c "$(declare -f edit_keys); set -euo pipefail; edit_keys "'"$@"' _ "$@"
  fi
}
# --- end edit_keys

# --- data_grants: stdin — "<table>|<visible columns>|<hidden columns>|." lines of one database (columns
# separated by spaces, a leading ! — a name that is not plain); stdout — its GRANT lines for agent_data; stderr — the
# hidden columns and the skipped tables. "|" is no whitespace for read: an empty field stays empty, so a table of
# secrets only gets no grant at all; the "." field closes a whole line, so a "|" inside a name never shifts a field
# into place. A table with any name outside [A-Za-z0-9_] is skipped whole.
data_grants() {
  local db=$1 table visible secret end cols c
  while IFS='|' read -r table visible secret end; do
    if ! [[ $end == . && $table =~ ^[A-Za-z0-9_]+$ && $visible =~ ^([A-Za-z0-9_]+( [A-Za-z0-9_]+)*)?$ \
          && $secret =~ ^([A-Za-z0-9_]+( [A-Za-z0-9_]+)*)?$ ]]; then
      echo "data.read: $db.${table#!} skipped: a name is not plain" >&2
      continue
    fi
    if [ -z "$secret" ]; then
      echo "GRANT SELECT ON \`$db\`.\`$table\` TO 'agent_data'@'localhost';"
      continue
    fi
    echo "data.read: hidden $db.$table: $secret" >&2
    [ -n "$visible" ] || continue
    cols=""
    for c in $visible; do cols="$cols${cols:+, }\`$c\`"; done
    echo "GRANT SELECT ($cols) ON \`$db\`.\`$table\` TO 'agent_data'@'localhost';"
  done
}
# --- end data_grants

if [ "$mode" = check ]; then
  blockers=0
  say() { printf '%-26s %s\n' "$1" "$2"; }
  for tool in sudo visudo runuser useradd usermod sha256sum openssl; do  # openssl: the exec gate verifies the owner's signature with it
    if command -v "$tool" >/dev/null; then say "$tool" ok; else say "$tool" "MISSING"; blockers=1; fi
  done
  for tool in sshd ufw fail2ban-client needrestart; do
    if command -v "$tool" >/dev/null; then say "$tool" ok; else say "$tool" "missing (snapshot/check: absent or n/a)"; fi
  done
  if [ -r /var/log/auth.log ]; then say "auth.log" ok; else say "auth.log" "missing: the monitor will use journalctl"; fi
  if py=$(find_python); then say python "$py ($("$py" -V 2>&1))"; else say python "MISSING python3 ≥ 3.6"; blockers=1; fi
  say "exec user" "$exec_user ($exec_home)"
  sshd_t=$(sshd -T -C user=agent-ro,host=$stage_alias,addr=$STAGE_IP 2>/dev/null) || sshd_t=""
  if [ -z "$sshd_t" ]; then
    say "BLOCKER" "sshd -T failed: the sshd settings are not checked (no sshd or a configuration error)"; blockers=1
  else
    for key in allowusers allowgroups denyusers denygroups authorizedkeysfile permitrootlogin usepam; do
      say "sshd $key" "$(printf '%s\n' "$sshd_t" | awk -v k="$key" '$1 == k {$1 = ""; print}' | xargs)"
    done
    if printf '%s\n' "$sshd_t" | grep -qE '^(allowusers|allowgroups) '; then
      say "BLOCKER" "sshd has AllowUsers/AllowGroups and agent-ro is not in them; editing sshd is the owner's decision"; blockers=1
    fi
    if [ "$exec_user" = root ] && printf '%s\n' "$sshd_t" | grep -qx 'permitrootlogin no'; then
      say "BLOCKER" "PermitRootLogin no: the root exec key cannot log in"; blockers=1
    fi
    if ! printf '%s\n' "$sshd_t" | grep -q '^authorizedkeysfile .*\.ssh/authorized_keys'; then
      say "BLOCKER" "AuthorizedKeysFile is non-standard"; blockers=1
    fi
  fi
  say "SELinux" "$(getenforce 2>/dev/null || echo none)"
  banned=$(fail2ban-client status sshd 2>/dev/null | grep 'Banned IP' || true)
  say "fail2ban sshd" "${banned:-no jail or empty}"
  case $banned in *"$STAGE_IP"*) say "BLOCKER" "stage IP $STAGE_IP is banned by fail2ban"; blockers=1 ;; esac
  say "free" "$(df -h / /tmp | awk 'NR > 1 {print $6 ": " $4}' | xargs)"
  say "MySQL debian.cnf" "$([ -f /etc/mysql/debian.cnf ] && echo present || echo missing)"
  say "agent-ro" "$(id agent-ro 2>/dev/null || echo none)"
  say "exec gate" "$([ -f "$EXEC_GATE_JSON" ] && echo "installed ($(tr -d '\n' < "$EXEC_GATE_JSON" | cut -c1-120))" || echo none)"
  say "exec policy" "$([ -f "$EXEC_POLICY" ] && echo "installed (sha256 $(sha256sum "$EXEC_POLICY" | cut -c1-12))" || echo none)"
  say "app flag helper" "$([ -f "$APP_FLAG" ] && echo "installed (sha256 $(sha256sum "$APP_FLAG" | cut -c1-12))" || echo none)"
  say "typed op helper" "$([ -f "$TYPED_OP" ] && echo "installed (sha256 $(sha256sum "$TYPED_OP" | cut -c1-12))" || echo none)"
  say "script steps" "$([ -e "$EXEC_GATE_DIR/script-off" ] && echo "off (gate.script)" || echo on)"
  say "test environments" "$([ -f "$TEST_ENVS" ] && echo "a list is installed (sha256 $(sha256sum "$TEST_ENVS" | cut -c1-12)): its steps run unsigned" || echo none)"
  for path in $(test_env_reads "$TEST_ENVS" 2>/dev/null || true); do
    if runuser -u agent-ro -- test -r "$path" -a -x "$path" 2>/dev/null; then say "test env read" "$path: open to agent-ro"
    else say "test env read" "$path: NOT open to agent-ro (install again with --test-envs)"; fi
  done
  exit "$blockers"
fi

if [ "$mode" = remove ]; then
  # agent-ro first loses its login, then its processes, then the user; MySQL is not fatal
  refuse_symlinks
  rm -f /home/agent-ro/.ssh/authorized_keys
  exec_blob=$(key_blob "$exec_key")
  if grep -qsF -e "$EXEC_COMMENT" ${exec_blob:+-e "$exec_blob"} "$exec_auth"; then
    exec_edit_keys "$exec_auth" "" "$EXEC_COMMENT" "$exec_blob"
  fi
  # shellcheck disable=SC2046 # the test environments' reads close with the list; the gate prints plain paths
  test_env_acl revoke $(test_env_reads "$TEST_ENVS" 2>/dev/null || true)
  rm -f /etc/sudoers.d/agent-ro "$GATE" "$SNAPSHOT" "$USAGE" "$FORBIDDEN" "$EXEC_GATE" "$EXEC_POLICY" "$APP_FLAG" "$TYPED_OP" "$EXEC_GATE_DIR/script-off" "$EXEC_GATE_JSON" "$EXEC_SIGNERS" "$TEST_ENVS"
  rm -rf "$EXEC_USED"
  rmdir "$FORBIDDEN_DIR" "$EXEC_GATE_DIR" "$(dirname "$EXEC_USED")" 2>/dev/null || true
  if [ -f /etc/mysql/debian.cnf ] && command -v mysql >/dev/null; then
    mysql_root -e "DROP USER IF EXISTS 'agent_ro'@'localhost'" || echo "warning: DROP USER agent_ro failed" >&2
    mysql_root -e "DROP USER IF EXISTS 'agent_data'@'localhost'" || echo "warning: DROP USER agent_data failed" >&2
  fi
  if id agent-ro >/dev/null 2>&1; then
    pkill -u agent-ro || true
    sleep 1
    userdel -r -f agent-ro 2>/dev/null || userdel -f agent-ro
  fi
  echo "rollback on $(hostname): agent-ro, the gate, sudoers and the exec key removed"
  exit 0
fi

[ -n "$gate_src" ] && [ -n "$snapshot_src" ] && [ -n "$usage_src" ] && [ -n "$ro_key" ] && [ -n "$exec_key" ] || { echo "--gate, --snapshot, --usage, --ro-key, --exec-key are required" >&2; exit 2; }
case $ro_key in "ssh-ed25519 "*" $RO_COMMENT") ;; *) echo "--ro-key: expected ssh-ed25519 … $RO_COMMENT" >&2; exit 2 ;; esac
[[ $exec_key =~ ^ssh-ed25519\ [A-Za-z0-9+/]+={0,2}\ agent-exec@[A-Za-z0-9-]+$ && $exec_key == *" $EXEC_COMMENT" ]] \
  || { echo "--exec-key: expected ssh-ed25519 … $EXEC_COMMENT" >&2; exit 2; }
# the data key comes with its databases, or neither; the values reach SQL and authorized_keys — strictly
if [ -n "$data_key$data_dbs$data_hidden" ]; then
  [ -n "$data_key" ] && [ -n "$data_dbs" ] || { echo "--data-key and --data-dbs come together" >&2; exit 2; }
  [[ $data_key =~ ^ssh-ed25519\ [A-Za-z0-9+/]+={0,2}\ agent-data@[A-Za-z0-9-]+$ && $data_key == *" $DATA_COMMENT" ]] \
    || { echo "--data-key: expected ssh-ed25519 … $DATA_COMMENT" >&2; exit 2; }
  [[ $data_dbs =~ ^[A-Za-z0-9_]+( [A-Za-z0-9_]+)*$ ]] || { echo "--data-dbs: database names separated by spaces" >&2; exit 2; }
  [[ -z $data_hidden || $data_hidden =~ ^[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+( [A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+)*$ ]] \
    || { echo "--data-hidden: <db>.<table>.<column> separated by spaces" >&2; exit 2; }
  [ -f /etc/mysql/debian.cnf ] || { echo "no /etc/mysql/debian.cnf: how root logs in to MySQL on $(hostname) is for the owner to decide" >&2; exit 1; }
fi
# the exec gate comes with this server's alias and the cabinet's rp_id and origin (they go into gate.json,
# written by python — checked strictly all the same); the mode is optional (an installed one stays)
[ -n "$exec_gate_src" ] || { echo "--exec-gate <file> is required" >&2; exit 2; }
[ -n "$exec_policy_src" ] || { echo "--exec-policy <file> is required (KD-9244: the gate refuses everything without it)" >&2; exit 2; }
# the deployment's site units (fnmatch patterns of the catalog's critical_units) go into gate.json as a JSON list
[[ -z $critical_units || $critical_units =~ ^[A-Za-z0-9*][A-Za-z0-9@._*-]{0,120}( [A-Za-z0-9*][A-Za-z0-9@._*-]{0,120})*$ ]] \
  || { echo "--critical-units: unit patterns separated by spaces" >&2; exit 2; }
[[ $alias =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || { echo "--alias <this server's alias> is required" >&2; exit 2; }
[[ $rp_id =~ ^[a-z0-9.-]+$ ]] || { echo "--rp-id <the cabinet's host> is required" >&2; exit 2; }
[[ $origin =~ ^https://[a-z0-9.-]+$ ]] || { echo "--origin https://<the cabinet's host> is required" >&2; exit 2; }
[[ -z $exec_mode || $exec_mode =~ ^(observe|enforce)$ ]] || { echo "--exec-mode observe|enforce" >&2; exit 2; }
command -v openssl >/dev/null || { echo "no openssl: the exec gate verifies the owner's signature with it" >&2; exit 1; }
py=$(find_python) || { echo "no python3 ≥ 3.6" >&2; exit 1; }
"$py" -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$gate_src"
"$py" -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$snapshot_src"
"$py" -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$usage_src"
"$py" -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$exec_gate_src"
"$py" -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$exec_policy_src"
# the first line of the source is replaced by a shebang (tail -n +2): without a "#!" a line of code would be lost
for src in "$gate_src" "$snapshot_src" "$usage_src" "$exec_gate_src"; do
  [ "$(head -c2 "$src")" = '#!' ] || { echo "$src: no shebang" >&2; exit 1; }
done

# 1. agent-ro: login by key only (the password '*' is not "locked" for sshd), journals through groups
id agent-ro >/dev/null 2>&1 || useradd --create-home --home-dir /home/agent-ro --shell /bin/sh agent-ro
if [ "$(getent shadow agent-ro | cut -d: -f2)" != '*' ]; then usermod -p '*' agent-ro; fi
# the home belongs to root: otherwise agent-ro (having bypassed the gateway) renames .ssh and makes its own
# authorized_keys without restrict,command=
chown root:root /home/agent-ro
chmod 0755 /home/agent-ro
for group in adm systemd-journal; do
  if getent group "$group" >/dev/null; then usermod -aG "$group" agent-ro; fi
done

# 2. The gateway (the first line is the python found, with -I: sys.path[0] = /usr/local/bin, which the staff
# group can write on Ubuntu)
tmp=$(mktemp)
{ echo "#!$py -I"; tail -n +2 "$gate_src"; } > "$tmp"
install -o root -g root -m 0755 "$tmp" "$GATE"
rm -f "$tmp"
if [ -n "$forbidden_src" ]; then
  # checked by the gate's own reader before it replaces the list: a list the gate would refuse never lands
  "$py" -I -c 'import json, re, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert isinstance(d, dict) and d.get("schema_version") == 1 and isinstance(d.get("forbidden"), list)
assert all(isinstance(p, str) and p for p in d["forbidden"])
[re.compile(p, re.ASCII) for p in d["forbidden"]]' "$forbidden_src" \
    || { echo "ERROR: --forbidden $forbidden_src: expected {schema_version: 1, forbidden: [regex, …]}" >&2; exit 1; }
  install -d -o root -g root -m 0755 "$FORBIDDEN_DIR"
  install -o root -g root -m 0644 "$forbidden_src" "$FORBIDDEN"
fi

# 2b. The security snapshot (root, read-only) — before sudoers: its sudoers line appears once the binary exists
tmp=$(mktemp)
{ echo "#!$py -I"; tail -n +2 "$snapshot_src"; } > "$tmp"
install -o root -g root -m 0755 "$tmp" "$SNAPSHOT"
rm -f "$tmp"

# 2c. Hindsight usage (root, read-only, numbers only) — before sudoers as well
tmp=$(mktemp)
[ "$timezone_name" = UTC ] || [ -f "/usr/share/zoneinfo/$timezone_name" ] \
  || { rm -f "$tmp"; echo "ERROR: zone $timezone_name is not in /usr/share/zoneinfo" >&2; exit 1; }
{ echo "#!$py -I"; tail -n +2 "$usage_src" | sed "s|^ZONE = \"UTC\"  #|ZONE = \"$timezone_name\"  #|"; } > "$tmp"
[ "$(grep -c "^ZONE = \"$timezone_name\"  #" "$tmp")" = 1 ] || { rm -f "$tmp"; echo "ERROR: the zone is not written into $usage_src" >&2; exit 1; }
install -o root -g root -m 0755 "$tmp" "$USAGE"
rm -f "$tmp"

# 2d. Exec gate: the exec key's forced command. The registry is read by the gate's own reader before it
# lands (a registry the gate would refuse never replaces a working one); without --exec-signers the installed one
# stays, so the first install needs it. The mode stays unless given; the first install observes.
# The registry and the cabinet's rp_id/origin come from the factory host, the very host the gate guards against:
# once this server trusts a set of keys and a cabinet, a different one is refused (old -> new printed) unless the
# owner's machine says --replace-signers.
# the policy lands first — a new gate refuses everything without it; a module, not a command (0644)
install -o root -g root -m 0644 "$exec_policy_src" "$EXEC_POLICY"
tmp=$(mktemp)
{ echo "#!$py -I"; tail -n +2 "$exec_gate_src"; } > "$tmp"
install -o root -g root -m 0755 "$tmp" "$EXEC_GATE"
rm -f "$tmp"
install -d -o root -g root -m 0755 "$EXEC_GATE_DIR"
if [ -n "$exec_signers_src" ]; then
  "$py" -I "$EXEC_GATE" --check-signers "$exec_signers_src" \
    || { echo "ERROR: --exec-signers $exec_signers_src: the gate refuses this registry; nothing replaced" >&2; exit 1; }
fi
if [ -f "$EXEC_SIGNERS" ]; then
  if ! "$py" -I -c '
import json, sys
old_path, new_path, rp_id, origin = sys.argv[1:5]


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def entries(reg):
    return [s for s in reg.get("signers", []) if isinstance(s, dict)]


def keys(reg):
    return sorted((str(s.get("user")), str(s.get("credential_id")), str(s.get("public_key"))) for s in entries(reg))


def names(reg):
    return ", ".join("%s %s... (%s)" % (s.get("user"), str(s.get("credential_id"))[:8], s.get("name"))
                     for s in entries(reg)) or "-"


old = load(old_path)
new = load(new_path) if new_path else old
changed = []
if (old.get("rp_id"), old.get("origin")) != (rp_id, origin):
    changed.append("cabinet: %s %s -> %s %s" % (old.get("rp_id"), old.get("origin"), rp_id, origin))
if keys(old) != keys(new):
    changed.append("signers: %s -> %s" % (names(old), names(new)))
for line in changed:
    sys.stderr.buffer.write((line + "\n").encode("utf-8"))
sys.exit(1 if changed else 0)
' "$EXEC_SIGNERS" "$exec_signers_src" "$rp_id" "$origin"; then
    [ -n "$replace_signers" ] || { echo "ERROR: the signing keys or the cabinet this server trusts would change (above); nothing replaced — if the owner means it, run again with --replace-signers" >&2; exit 1; }
    echo "replacing the signers registry of $(hostname) (--replace-signers)"
  fi
fi
if [ -n "$exec_signers_src" ]; then
  install -o root -g root -m 0644 "$exec_signers_src" "$EXEC_SIGNERS"
fi
[ -f "$EXEC_SIGNERS" ] || { echo "ERROR: no signers registry on $(hostname): --exec-signers <file> is required on the first install" >&2; exit 1; }
if [ -z "$exec_mode" ]; then
  exec_mode=$("$py" -I -c 'import json, sys
try:
    m = json.load(open(sys.argv[1], encoding="utf-8")).get("mode")
except (OSError, ValueError, AttributeError):
    m = None
print(m if m in ("observe", "enforce") else "observe")' "$EXEC_GATE_JSON")
fi
tmp=$(mktemp)
# critical_units only when given (an empty value — an empty list); absent — every unit's stop is critical
"$py" -I -c 'import json, sys
gate = {"alias": sys.argv[1], "rp_id": sys.argv[2], "origin": sys.argv[3], "mode": sys.argv[4]}
if sys.argv[5]:
    gate["critical_units"] = sys.argv[6].split()
print(json.dumps(gate, sort_keys=True))' \
  "$alias" "$rp_id" "$origin" "$exec_mode" "$critical_units_given" "$critical_units" > "$tmp"
install -o root -g root -m 0644 "$tmp" "$EXEC_GATE_JSON"
rm -f "$tmp"
# the exec user records the critical actions it ran (once each): its directory, nobody else's
install -d -o root -g root -m 0755 "$(dirname "$EXEC_USED")"
install -d -o "$exec_user" -g "$(id -gn "$exec_user")" -m 0700 "$EXEC_USED"

# 2e. App flag helper: optional — an install without --app-flag leaves an installed helper alone. Root's
# and read-only for the application's account that runs it (python3 -I <this file>), so a step cannot rewrite it
if [ -n "$app_flag_src" ]; then
  "$py" -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$app_flag_src"
  install -d -o root -g root -m 0755 "$(dirname "$APP_FLAG")"
  install -o root -g root -m 0755 "$app_flag_src" "$APP_FLAG"
  if command -v restorecon >/dev/null; then restorecon "$APP_FLAG" || true; fi
fi

# 2e'. Typed op helper: optional the same way. Root's, run by the gate as root (python3 -I <this file>)
if [ -n "$typed_op_src" ]; then
  "$py" -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$typed_op_src"
  install -d -o root -g root -m 0755 "$(dirname "$TYPED_OP")"
  install -o root -g root -m 0755 "$typed_op_src" "$TYPED_OP"
  if command -v restorecon >/dev/null; then restorecon "$TYPED_OP" || true; fi
fi

# 2f. Test environments: optional — an install without --test-envs leaves an installed list alone. The
# gate checks the list by its own rules before it lands; a list with no entries removes the installed one, and the
# gate asks for the owner's signature on every step again
if [ -n "$test_envs_src" ]; then
  envs=$("$py" -I "$EXEC_GATE" --check-test-envs "$test_envs_src") \
    || { echo "ERROR: --test-envs $test_envs_src: the gate refuses this list; nothing replaced" >&2; exit 1; }
  # the installed list's reads close first, then the new list's open — a read the catalog dropped stays
  # closed, one it kept opens again (traverse two reads share survives)
  old_reads=$(test_env_reads "$TEST_ENVS" || true)
  new_reads=$("$py" -I "$EXEC_GATE" --test-env-reads "$test_envs_src")
  # shellcheck disable=SC2086 # one directory per word: the gate prints plain paths only
  test_env_acl revoke $old_reads
  if [ "$envs" = 0 ]; then
    rm -f "$TEST_ENVS"
  else
    install -o root -g root -m 0644 "$test_envs_src" "$TEST_ENVS"
    # shellcheck disable=SC2086
    test_env_acl grant $new_reads
  fi
fi

# 3. sudoers: the exact forms from the gateway (the snapshot — with the sha256 of the file just installed), visudo
# checks it before installing; the gateway runs as root with -I, so that a shlex.py planted in /usr/local/bin does not run
tmp=$(mktemp)
"$py" -I "$GATE" --sudoers > "$tmp"
visudo -c -f "$tmp" >/dev/null
install -o root -g root -m 0440 "$tmp" /etc/sudoers.d/agent-ro
rm -f "$tmp"

# 4. authorized_keys of agent-ro belongs to root — agent-ro cannot change it
refuse_symlinks
install -d -o root -g root -m 0755 /home/agent-ro/.ssh
tmp=$(mktemp /home/agent-ro/.ssh/.ak.XXXXXX)
trap 'rm -f "$tmp"' EXIT
printf 'restrict,command="%s",from="%s" %s\n' "$GATE" "$STAGE_IP" "$ro_key" > "$tmp"
# kd-data's key — the same user, MySQL only (agent-ro-gate --data); the sysadmin's line above stays
if [ -n "$data_key" ]; then printf 'restrict,command="%s --data",from="%s" %s\n' "$GATE" "$STAGE_IP" "$data_key" >> "$tmp"; fi
chown root:root "$tmp"
chmod 0644 "$tmp"
mv -f "$tmp" /home/agent-ro/.ssh/authorized_keys

# 5. the exec key: only its own line; foreign lines (an rrsync mirror of backups, for one) are left alone
# made by the exec user itself (its ~/.ssh is not root's to write into, see exec_edit_keys)
runuser -u "$exec_user" -- sh -c 'umask 077; [ -d "$1" ] || mkdir -m 0700 "$1"; [ -f "$2" ] || : > "$2"' _ "$exec_home/.ssh" "$exec_auth"
# the key's line is the gate's forced command (restrict = no pty, no forwarding, no user rc; the executor
# runs ssh -T). The whole line is compared, not only the key: a line from before the gate is rewritten.
# every other line with the key's comment or its blob goes too — the same key under another comment (or none)
# would log in past the gate; the file is left alone only when the gate's line is the one line of that key.
exec_line="restrict,command=\"$EXEC_GATE\",from=\"$STAGE_IP\" $exec_key"
exec_blob=$(key_blob "$exec_key")
if ! grep -qxF -- "$exec_line" "$exec_auth" \
    || [ "$(grep -cF -e "$EXEC_COMMENT" -e "$exec_blob" "$exec_auth" || true)" -ne 1 ]; then
  exec_edit_keys "$exec_auth" "$exec_line" "$EXEC_COMMENT" "$exec_blob"
fi

# 6. SELinux (AlmaLinux): labels for the new files
if command -v restorecon >/dev/null; then restorecon -R /home/agent-ro/.ssh "$GATE" "$SNAPSHOT" "$USAGE" "$EXEC_GATE" "$EXEC_POLICY" "$EXEC_GATE_DIR" "$(dirname "$EXEC_USED")" || true; fi

# 7. MySQL: agent_ro — only SELECT on the list of tables, PROCESS and performance_schema
if [ -n "$mysql_db" ]; then
  [[ $mysql_db =~ ^[A-Za-z0-9_]+$ ]] || { echo "--mysql-db: invalid name" >&2; exit 2; }
  # the tables for agent_ro come from the deployment's inventory (mysql_tables); tables with secrets are not among them
  [[ $mysql_tables =~ ^[A-Za-z0-9_]+( [A-Za-z0-9_]+)*$ ]] || { echo "--mysql-tables: table names separated by spaces" >&2; exit 2; }
  [ -f /etc/mysql/debian.cnf ] || { echo "no /etc/mysql/debian.cnf: how root logs in to MySQL on $(hostname) is for the owner to decide" >&2; exit 1; }
  refuse_symlinks
  cnf=/home/agent-ro/.my.cnf
  # the password of the agent-readable .my.cnf is substituted into SQL run as MySQL root: it is taken only when it
  # is wholly in the generator's format
  pw_re='^Kd-[A-Za-z0-9]{28}9a$'
  pw=""
  if [ -f "$cnf" ] && mysql_root -N -B -e "SELECT 1 FROM mysql.user WHERE user='agent_ro' AND host='localhost'" | grep -qx 1; then
    pw=$(sed -n 's/^password="\(.*\)"$/\1/p' "$cnf")  # повторный прогон: пароль не меняем
    if ! [[ $pw =~ $pw_re ]]; then
      echo "warning: the password in .my.cnf is not in the generator's format; creating a new one" >&2
      pw=""
    fi
  fi
  if [ -z "$pw" ]; then
    pw=$("$py" -c 'import secrets, string; a = string.ascii_letters + string.digits; print("Kd-" + "".join(secrets.choice(a) for _ in range(28)) + "9a")')
  fi
  [[ $pw =~ $pw_re ]] || { echo "the agent_ro password failed the format check" >&2; exit 1; }
  {
    echo "SET SESSION lock_wait_timeout=10;"
    echo "CREATE USER IF NOT EXISTS 'agent_ro'@'localhost' IDENTIFIED BY '$pw';"
    echo "ALTER USER 'agent_ro'@'localhost' IDENTIFIED BY '$pw' WITH MAX_USER_CONNECTIONS 3;"
  } | mysql_root
  tmp=$(mktemp /home/agent-ro/.my.cnf.XXXXXX)
  trap 'rm -f "$tmp"' EXIT
  printf '[client]\nuser=agent_ro\npassword="%s"\n[mysql]\ndatabase=%s\n' "$pw" "$mysql_db" > "$tmp"
  chown agent-ro:agent-ro "$tmp"
  chmod 0600 "$tmp"
  mv -f "$tmp" "$cnf"
  existing=$(mysql_root -N -B -e "SELECT table_name FROM information_schema.tables WHERE table_schema='$mysql_db'")
  {
    echo "SET SESSION lock_wait_timeout=10;"
    echo "GRANT PROCESS ON *.* TO 'agent_ro'@'localhost';"
    echo "GRANT SELECT ON performance_schema.* TO 'agent_ro'@'localhost';"
    for table in $mysql_tables; do
      if printf '%s\n' "$existing" | grep -qx "$table"; then
        echo "GRANT SELECT ON \`$mysql_db\`.\`$table\` TO 'agent_ro'@'localhost';"
      else
        echo "no table $mysql_db.$table, skipped" >&2
      fi
    done
  } | mysql_root
fi

# 7b. MySQL agent_data (kd-data): SELECT on every table of --data-dbs, but per column where a table has a
# secret column — a SELECT of that column is refused by MySQL itself; 2 connections. The grants are rebuilt on every
# run (a new table, a new hidden column); the hidden columns are printed for the owner.
if [ -n "$data_dbs" ]; then
  refuse_symlinks
  pw_re='^Kd-[A-Za-z0-9]{28}9a$'
  pw=""
  if [ -f "$DATA_CNF" ] && mysql_root -N -B -e "SELECT 1 FROM mysql.user WHERE user='agent_data' AND host='localhost'" | grep -qx 1; then
    pw=$(sed -n 's/^password="\(.*\)"$/\1/p' "$DATA_CNF")  # повторный прогон: пароль не меняем
    [[ $pw =~ $pw_re ]] || pw=""
  fi
  if [ -z "$pw" ]; then
    pw=$("$py" -c 'import secrets, string; a = string.ascii_letters + string.digits; print("Kd-" + "".join(secrets.choice(a) for _ in range(28)) + "9a")')
  fi
  [[ $pw =~ $pw_re ]] || { echo "the agent_data password failed the format check" >&2; exit 1; }
  {
    echo "SET SESSION lock_wait_timeout=10;"
    echo "CREATE USER IF NOT EXISTS 'agent_data'@'localhost' IDENTIFIED BY '$pw';"
    echo "ALTER USER 'agent_data'@'localhost' IDENTIFIED BY '$pw' WITH MAX_USER_CONNECTIONS 2;"
  } | mysql_root
  tmp=$(mktemp /home/agent-ro/.my-data.cnf.XXXXXX)
  trap 'rm -f "$tmp"' EXIT
  printf '[client]\nuser=agent_data\npassword="%s"\n' "$pw" > "$tmp"
  chown agent-ro:agent-ro "$tmp"
  chmod 0600 "$tmp"
  mv -f "$tmp" "$DATA_CNF"
  hidden_list=""
  for h in $data_hidden; do hidden_list="$hidden_list${hidden_list:+,}'$h'"; done
  grants=$(mktemp)
  echo "SET SESSION lock_wait_timeout=10;" > "$grants"
  echo "REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'agent_data'@'localhost';" >> "$grants"
  for db in $data_dbs; do
    if ! mysql_root -N -B -e "SELECT 1 FROM information_schema.schemata WHERE schema_name='$db'" | grep -qx 1; then
      echo "data.read: no database $db on $(hostname), skipped" >&2; continue
    fi
    # one line per table: [!]<table>|<visible columns>|<hidden columns>|. (! — a table or column name that is not plain)
    mysql_root -N -B -e "SET SESSION group_concat_max_len=1048576;
      SELECT CONCAT(IF(SUM(column_name NOT REGEXP '^[A-Za-z0-9_]+\$') OR table_name NOT REGEXP '^[A-Za-z0-9_]+\$',
                       '!', ''), table_name, '|',
        IFNULL(GROUP_CONCAT(IF(h, NULL, column_name) ORDER BY ordinal_position SEPARATOR ' '), ''), '|',
        IFNULL(GROUP_CONCAT(IF(h, column_name, NULL) ORDER BY ordinal_position SEPARATOR ' '), ''), '|.')
      FROM (SELECT table_name, column_name, ordinal_position,
              (LOWER(column_name) REGEXP '$DATA_SECRET_RE'
               OR CONCAT(table_schema, '.', table_name, '.', column_name) IN (${hidden_list:-''})) AS h
            FROM information_schema.columns WHERE table_schema='$db') c
      GROUP BY table_name ORDER BY table_name" | data_grants "$db" >> "$grants"
  done
  mysql_root < "$grants"
  rm -f "$grants"
fi

# 8. The gateway's self-check as agent-ro
echo "--- self-check on $(hostname)"
out=$(runuser -u agent-ro -- env SSH_ORIGINAL_COMMAND=uptime "$GATE" 2>&1) || true
case $out in
  *"load average"*) echo "$out" ;;
  *) echo "ERROR: the gate did not run uptime: $out" >&2; exit 1 ;;
esac
if runuser -u agent-ro -- env SSH_ORIGINAL_COMMAND='cat /etc/shadow' "$GATE" >/dev/null 2>&1; then
  echo "ERROR: the gate let cat /etc/shadow through" >&2; exit 1
fi
out=$(runuser -u agent-ro -- env SSH_ORIGINAL_COMMAND=agent-sec-snapshot "$GATE" 2>&1) || true
case $out in
  "# agent-sec-snapshot v1"*)
    echo "security snapshot: $(printf '%s\n' "$out" | grep -c "$(printf '\t')") lines"
    printf '%s\n' "$out" | grep "$(printf '\terror\t')" | sed 's/^/  section with an error: /' || true ;;
  *) echo "ERROR: the gate did not run agent-sec-snapshot: $out" >&2; exit 1 ;;
esac
out=$(runuser -u agent-ro -- env SSH_ORIGINAL_COMMAND=agent-hindsight-usage "$GATE" 2>&1) || true
case $out in
  "# agent-hindsight-usage v1"*) echo "Hindsight usage: $(printf '%s\n' "$out" | grep -c "^unit$(printf '\t')") units in the output" ;;
  *) echo "ERROR: the gate did not run agent-hindsight-usage: $out" >&2; exit 1 ;;
esac
if [ -n "$data_key" ]; then
  # the data key's mode: nothing but mysql, and mysql as agent_data
  if runuser -u agent-ro -- env SSH_ORIGINAL_COMMAND=uptime "$GATE" --data >/dev/null 2>&1; then
    echo "ERROR: the gate's data mode let uptime through" >&2; exit 1
  fi
  out=$(runuser -u agent-ro -- env SSH_ORIGINAL_COMMAND="mysql -N -e 'SELECT CURRENT_USER()'" "$GATE" --data 2>&1) || true
  case $out in
    agent_data@localhost*) echo "data mode: mysql as agent_data" ;;
    *) echo "ERROR: the gate's data mode did not reach MySQL as agent_data: $out" >&2; exit 1 ;;
  esac
fi
# the exec gate as the exec user — an empty envelope is refused in both modes (126, a refusal); then its own
# report (mode, alias, signers, openssl — a missing openssl fails it)
rc=0; out=$(runuser -u "$exec_user" -- env SSH_ORIGINAL_COMMAND='agent-exec-gate run' "$EXEC_GATE" </dev/null 2>&1) || rc=$?
case "$rc:$out" in
  126:*отказ*) ;;
  *) echo "ERROR: the exec gate did not refuse an empty envelope (exit $rc): $out" >&2; exit 1 ;;
esac
# the policy's rule 1 — an unsigned plan envelope whose script writes into sudoers.d is refused by the
# policy in both modes (observe would run an unsigned one), before the signature is even read
probe='{"schema_version": 1, "kind": "plan", "task_id": "KD-1", "server": "probe", "step": 1, "command": "echo x > /etc/sudoers.d/x", "payload": {}, "proposal": {}, "grant": {}, "decision": {}, "assertion": null}'
rc=0; out=$(printf '%s' "$probe" | runuser -u "$exec_user" -- env SSH_ORIGINAL_COMMAND='agent-exec-gate run' "$EXEC_GATE" 2>&1) || rc=$?
case "$rc:$out" in
  126:*deploy_target.sh*) echo "exec policy: rule 1 refuses a write into sudoers.d" ;;
  *) echo "ERROR: the exec policy did not refuse a write into sudoers.d (exit $rc): $out" >&2; exit 1 ;;
esac
"$py" -I "$EXEC_GATE" --check | sed 's/^/exec gate /'
echo "done: agent-ro, the gate, sudoers, the exec key ($exec_user) behind agent-exec-gate ($exec_mode)${mysql_db:+, MySQL agent_ro → $mysql_db}${data_dbs:+, kd-data → $data_dbs}"
