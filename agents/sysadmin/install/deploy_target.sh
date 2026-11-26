#!/usr/bin/env bash
# From the owner's machine: the agent-ro gateway and the exec key → the server <alias> (the aliases of ~/.ssh/config
# match the inventory). The files come from the HEAD commit (not the work tree) and are verified on the target by sha256.
# usage:
#   bash agents/sysadmin/install/deploy_target.sh <alias> --check
#   bash agents/sysadmin/install/deploy_target.sh <alias> [--mysql-db <database>] [--exec-mode observe|enforce] [--replace-signers]
#   bash agents/sysadmin/install/deploy_target.sh <alias> --remove
# with the gate goes its policy (common/kd/exec_policy.py → /usr/local/bin/agent-exec-policy) and the
# overlay catalog's critical_units (into gate.json).
# every install puts agent-exec-gate and the owner's signing passkeys (kd-cabinet user signers on the
# factory
# host) on the target; --exec-mode switches the gate between logging (observe, the default of a first install) and
# refusing (enforce) a step without the owner's signature. A target keeps the keys and cabinet it first trusted:
# a changed registry is refused there (old -> new printed) unless --replace-signers says the owner means it.
set -euo pipefail
# M02: the sysadmin's own Linux user on the factory host holds the ro key and its known_hosts (servers_setup.sh).
# its name and the exec user's come from the host's naming, checked here before they reach any command.
# the factory host (alias, IP) and the inventory come from the deployment overlay: KD_DEPLOY is its
# checkout (default — this repository), host.json from it unless KD_HOST_JSON names another file.
cd "$(git rev-parse --show-toplevel)"
DEPLOY_TREE=${KD_DEPLOY:-.}
export KD_HOST_JSON=${KD_HOST_JSON:-$DEPLOY_TREE/host/host.json}
host_value() { ${PYTHON:-python} -I common/kd/naming.py host "$1"; }
STAGE=$(host_value stage_alias) && STAGE_IP=$(host_value stage_ip) && TIMEZONE=$(host_value timezone) \
  || { echo "could not read the factory host's values: KD_DEPLOY=<overlay checkout> or KD_HOST_JSON=<host.json>" >&2; exit 1; }
[[ $STAGE =~ ^[A-Za-z0-9][A-Za-z0-9-]*$ && $STAGE_IP =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] \
  || { echo "invalid factory host values: $STAGE, $STAGE_IP" >&2; exit 1; }
stage_user() { ssh "$STAGE" /usr/bin/python3 -I /opt/digipaddock/common/kd/naming.py user "$@"; }
target=${1:?usage: deploy_target.sh <alias> [--check|--remove|[--mysql-db <db>] [--exec-mode observe|enforce] [--replace-signers]]}; shift
[[ $target =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || { echo "invalid alias: $target" >&2; exit 2; }
# mode "" installs; --mysql-db, --exec-mode and --replace-signers refine an install, --check and --remove go alone
mode="" mysql_args="" exec_mode="" replace_signers=""
while [ $# -gt 0 ]; do
  case $1 in
    --check|--remove) [ $# -eq 1 ] && [ -z "$mysql_args$exec_mode$replace_signers" ] || { echo "$1 takes no other arguments" >&2; exit 2; }
                      mode=$1; shift ;;
    --mysql-db) [[ ${2:-} =~ ^[A-Za-z0-9_]+$ ]] || { echo "--mysql-db <db>: letters, digits and _ only" >&2; exit 2; }
                # the tables agent_ro reads are mysql_tables of the overlay's inventory
                tables=$(git -C "$DEPLOY_TREE" show HEAD:agents/sysadmin/inventory.json | ${PYTHON:-python} -c "import json, sys; print(' '.join(json.loads(sys.stdin.buffer.read().decode('utf-8')).get('mysql_tables', [])))") \
                  || { echo "could not read the overlay's inventory.json" >&2; exit 1; }
                [[ $tables =~ ^[A-Za-z0-9_]+( [A-Za-z0-9_]+)*$ ]] || { echo "inventory.json: mysql_tables must be a non-empty list of table names" >&2; exit 1; }
                mysql_args="--mysql-db $2 --mysql-tables '$tables'"; shift 2 ;;
    --exec-mode) [[ ${2:-} =~ ^(observe|enforce)$ ]] || { echo "--exec-mode observe|enforce" >&2; exit 2; }
                 exec_mode=$2; shift 2 ;;
    --replace-signers) replace_signers=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -z "$(git status --porcelain)" ] || { echo "the working tree is not clean: commit first" >&2; exit 1; }

# The keys are read from the factory host and checked LOCALLY, before any substitution into a command for the
# target: the agent's .pub is data the agent could replace (a single quote in it would leave the quoting and run
# as root on the target).
ro_re="^ssh-ed25519 [A-Za-z0-9+/]+={0,2} agent-ro@$STAGE\$"
exec_re="^ssh-ed25519 [A-Za-z0-9+/]+={0,2} agent-exec@$STAGE\$"
ro_key="" exec_key=""
if [ -z "$mode" ]; then
  SYSADMIN_USER=$(stage_user agent sysadmin) && STAGE_EXEC_USER=$(stage_user exec)     || { echo "could not read the user names on $STAGE (naming.py)" >&2; exit 1; }
  name_re='^[a-z][a-z0-9-]{0,31}$'
  [[ $SYSADMIN_USER =~ $name_re && $STAGE_EXEC_USER =~ $name_re ]]     || { echo "invalid user names on $STAGE: $SYSADMIN_USER, $STAGE_EXEC_USER" >&2; exit 1; }
  ro_key=$(ssh "$STAGE" runuser -u "$SYSADMIN_USER" -- cat "/home/$SYSADMIN_USER/.ssh/agent_ro_ed25519.pub") || { echo "could not read the ro key from $STAGE (has new_agent.sh --install sysadmin run?)" >&2; exit 1; }
  exec_key=$(ssh "$STAGE" cat "/home/$STAGE_EXEC_USER/.ssh/agent_exec_ed25519.pub") || { echo "could not read the exec key from $STAGE (has new_agent.sh --install sysadmin run?)" >&2; exit 1; }
  [[ $ro_key =~ $ro_re ]] || { echo "the ro key on the stage does not look like 'ssh-ed25519 <base64> agent-ro@$STAGE'; not sending it to the target" >&2; exit 1; }
  [[ $exec_key =~ $exec_re ]] || { echo "the exec key on the stage does not look like 'ssh-ed25519 <base64> agent-exec@$STAGE'; not sending it to the target" >&2; exit 1; }
fi

field() {  # field <ключ> — из inventory.json в HEAD overlay (UTF-8, с кириллицей; на Windows кодировка по умолчанию cp1251)
  git -C "$DEPLOY_TREE" show HEAD:agents/sysadmin/inventory.json | ${PYTHON:-python} -c "import json, sys; s = json.loads(sys.stdin.buffer.read().decode('utf-8'))['servers'][sys.argv[1]]; print(s.get(sys.argv[2], sys.argv[3]))" "$target" "$1" "${2:-}"
}
host_ip=$(field host)
exec_user=$(field exec_user root)
# values from inventory.json go into remote commands — they are checked strictly
ip_re='^((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])[.]){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])$'
user_re='^[a-z_][a-z0-9_-]{0,31}$'
[[ $host_ip =~ $ip_re ]] || { echo "inventory.json: host of server $target is not IPv4: $host_ip" >&2; exit 1; }
[[ $exec_user =~ $user_re ]] || { echo "inventory.json: invalid exec_user of server $target: $exec_user" >&2; exit 1; }
[ "$(ssh -G "$target" | awk '$1 == "hostname" {print $2}')" = "$host_ip" ] || { echo "alias $target in ~/.ssh/config does not point to $host_ip" >&2; exit 1; }
# the agent connects to $host_ip:22: another port in the owner's alias means the wrong sshd is checked and configured
port=$(ssh -G "$target" | awk '$1 == "port" {print $2}'); [ "$port" = 22 ] || { echo "alias $target in ~/.ssh/config points to port $port, but ssh-ro/ssh-exec use 22" >&2; exit 1; }
login_user=$(ssh -G "$target" | awk '$1 == "user" {print $2}')
sudo=""; [ "$login_user" = root ] || sudo="sudo -n"
echo ">>> $target ($host_ip; login $login_user, exec user $exec_user)"

# kd-data's key goes to a server whose inventory entry lists data_databases (and data_hidden_columns,
# <db>.<table>.<column>, the deployment's secret columns beyond the name rule); checked before any remote command
list_field() {  # list_field <key>: a list of the server's entry, space-separated
  git -C "$DEPLOY_TREE" show HEAD:agents/sysadmin/inventory.json | ${PYTHON:-python} -c "import json, sys; v = json.loads(sys.stdin.buffer.read().decode('utf-8'))['servers'][sys.argv[1]].get(sys.argv[2], []); assert isinstance(v, list) and all(isinstance(x, str) for x in v); print(' '.join(v))" "$target" "$1"
}
data_args="" STAGE_DATA_USER=""
if [ -z "$mode" ]; then
  data_dbs=$(list_field data_databases) && data_hidden=$(list_field data_hidden_columns) \
    || { echo "inventory.json: data_databases and data_hidden_columns of $target must be lists" >&2; exit 1; }
  if [ -n "$data_dbs" ]; then
    [[ $data_dbs =~ ^[A-Za-z0-9_]+( [A-Za-z0-9_]+)*$ ]] || { echo "inventory.json: data_databases of $target: names only" >&2; exit 1; }
    [[ -z $data_hidden || $data_hidden =~ ^[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+( [A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+)*$ ]] \
      || { echo "inventory.json: data_hidden_columns of $target: <db>.<table>.<column>" >&2; exit 1; }
    STAGE_DATA_USER=$(stage_user data) && [[ $STAGE_DATA_USER =~ $name_re ]] \
      || { echo "could not read the data user's name on $STAGE (naming.py)" >&2; exit 1; }
    data_key=$(ssh "$STAGE" cat "/home/$STAGE_DATA_USER/.ssh/agent_data_ed25519.pub") \
      || { echo "could not read the data key from $STAGE (has new_agent.sh --install of an agent with data.read run?)" >&2; exit 1; }
    [[ $data_key =~ ^ssh-ed25519\ [A-Za-z0-9+/]+={0,2}\ agent-data@$STAGE$ ]] \
      || { echo "the data key on the stage does not look like 'ssh-ed25519 <base64> agent-data@$STAGE'; not sending it to the target" >&2; exit 1; }
    data_args="--data-key '$data_key' --data-dbs '$data_dbs'${data_hidden:+ --data-hidden '$data_hidden'}"
  fi
fi

# the owner's signing passkeys from the cabinet on the factory host (root: kd-cabinet user signers — schema
# 1,
# the cabinet's rp_id and origin, 1..20 keys). Checked LOCALLY before any of it reaches the target: the values go
# into gate.json and the registry the gate trusts. No signer — the owner enables one in the cabinet first.
signers="" rp_id="" origin=""
if [ -z "$mode" ]; then
  signers=$(ssh "$STAGE" "/usr/local/bin/\$(/usr/bin/python3 -I /opt/digipaddock/common/kd/naming.py cmd kd-cabinet) user signers") \
    || { echo "no signing passkey: on $STAGE run kd-cabinet user signing <id> --credential <prefix> on (Настройки → Устройства shows the keys)" >&2; exit 1; }
  signer_lines=$(printf '%s\n' "$signers" | ${PYTHON:-python} -I -c '
import json, re, sys
d = json.loads(sys.stdin.buffer.read().decode("utf-8"))
b64 = re.compile(r"^[A-Za-z0-9_-]+$")
assert isinstance(d, dict) and d.get("schema_version") == 1, "schema_version"
assert isinstance(d.get("rp_id"), str) and re.match(r"^[a-z0-9.-]+$", d["rp_id"]), "rp_id"
assert isinstance(d.get("origin"), str) and re.match(r"^https://[a-z0-9.-]+$", d["origin"]), "origin"
s = d.get("signers")
assert isinstance(s, list) and 1 <= len(s) <= 20, "1..20 signers"
for e in s:
    assert isinstance(e, dict) and isinstance(e.get("name"), str), "signer"
    assert isinstance(e.get("user"), str) and re.match(r"^[a-z][a-z0-9_-]{0,31}$", e["user"]), "user"
    for k in ("credential_id", "public_key"):
        assert isinstance(e.get(k), str) and b64.match(e[k]), k
print(d["rp_id"])
print(d["origin"])
for e in s:
    print("signers: %s (%s, %s…)" % (" ".join(e["name"].split())[:40] or "-", e["user"], e["credential_id"][:8]))
') || { echo "the signers registry from $STAGE is malformed; not sending it to the target" >&2; exit 1; }
  rp_id=$(printf '%s\n' "$signer_lines" | sed -n 1p)
  origin=$(printf '%s\n' "$signer_lines" | sed -n 2p)
  [[ $rp_id =~ ^[a-z0-9.-]+$ && $origin =~ ^https://[a-z0-9.-]+$ ]] || { echo "odd rp_id/origin in the registry: $rp_id $origin" >&2; exit 1; }
  printf '%s\n' "$signer_lines" | tail -n +3  # for the owner to compare with Настройки → Устройства
fi

remote_dir=$(ssh "$target" "$sudo mktemp -d /tmp/kd-target.XXXXXX")
# The gateway, the snapshot, the Hindsight usage and agent-ro's sudoers from target_setup.sh are ours: the monitor's
# release window on the factory host takes them into the target's baseline without alarms. It did not open (the
# target is not attached yet, there is no monitor) — a warning only.
expect_open=0
monitor_window() {  # begin|end
  ssh "$STAGE" "h=\$(getent passwd $SYSADMIN_USER | cut -d: -f6) && [ -n \"\$h\" ] && runuser -u $SYSADMIN_USER -- env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=\"\$h\" LANG=C.UTF-8 /usr/local/bin/\$(/usr/bin/python3 -I /opt/digipaddock/common/kd/naming.py cmd kd-monitor) expect $1 $target --label 'deploy_target.sh $(git rev-parse --short HEAD)'"
}
cleanup() {
  if [ "$expect_open" = 1 ]; then monitor_window end || true; fi
  ssh "$target" "$sudo rm -rf '$remote_dir'" || true
}
trap cleanup EXIT

put() {  # put <путь в репозитории> <имя на цели>: из HEAD, с проверкой sha256 (tar есть не на всех целях)
  local want
  git show "HEAD:$1" | ssh "$target" "$sudo sh -c 'cat > $remote_dir/$2'"
  want=$(git show "HEAD:$1" | sha256sum | cut -d' ' -f1)
  ssh "$target" "$sudo sha256sum $remote_dir/$2" | grep -q "^$want " || { echo "sha256 of $2 does not match: the transfer is corrupted" >&2; exit 1; }
}

put agents/sysadmin/install/target_setup.sh target_setup.sh

if [ "$mode" = --check ]; then
  ssh "$target" "$sudo bash $remote_dir/target_setup.sh --check --exec-user $exec_user --stage-ip $STAGE_IP --stage-alias $STAGE"
  exit 0
fi

if [ "$mode" = --remove ]; then
  # the agent stops reaching the server at once, before the gateway is removed
  # the line is removed whole and literally (grep -vxF), the file is replaced atomically
  ssh "$STAGE" "set -e; f=/etc/digipaddock/servers.enabled; if [ -f \$f ]; then t=\$(mktemp /etc/digipaddock/.servers.XXXXXX); trap 'rm -f \"\$t\"' EXIT; grep -vxF -- '$target' \$f > \$t || [ \$? -eq 1 ]; chmod --reference=\$f \$t; mv -f \$t \$f; fi"
  # the exec key's lines go by its blob too (a line of the same key under another comment); a stage that
  # no longer has the key leaves the removal by comment only
  remove_key=""
  if STAGE_EXEC_USER=$(stage_user exec) && [[ $STAGE_EXEC_USER =~ ^[a-z][a-z0-9-]{0,31}$ ]] \
      && remove_key=$(ssh "$STAGE" cat "/home/$STAGE_EXEC_USER/.ssh/agent_exec_ed25519.pub") && [[ $remove_key =~ $exec_re ]]; then
    remove_args="--exec-key '$remove_key'"
  else
    remove_args=""
    echo "warning: the exec key is not readable on $STAGE; its lines on $target go by comment only" >&2
  fi
  ssh "$target" "$sudo bash $remote_dir/target_setup.sh --remove --exec-user $exec_user $remove_args --stage-ip $STAGE_IP --stage-alias $STAGE"
  exit 0
fi

put agents/sysadmin/gate/agent_ro_gate.py agent_ro_gate.py
put agents/sysadmin/gate/agent_sec_snapshot.py agent_sec_snapshot.py
put agents/sysadmin/gate/agent_hindsight_usage.py agent_hindsight_usage.py
# the deployment's own secret paths for the gate: host/gate_forbidden.json of the overlay, when it has one
forbidden_args=""
if git -C "$DEPLOY_TREE" cat-file -e HEAD:host/gate_forbidden.json 2>/dev/null; then
  git -C "$DEPLOY_TREE" show HEAD:host/gate_forbidden.json | ssh "$target" "$sudo sh -c 'cat > $remote_dir/gate_forbidden.json'"
  want=$(git -C "$DEPLOY_TREE" show HEAD:host/gate_forbidden.json | sha256sum | cut -d' ' -f1)
  ssh "$target" "$sudo sha256sum $remote_dir/gate_forbidden.json" | grep -q "^$want " \
    || { echo "sha256 of gate_forbidden.json does not match: the transfer is corrupted" >&2; exit 1; }
  forbidden_args="--forbidden $remote_dir/gate_forbidden.json"
fi
# the exec gate from HEAD and the registry from the cabinet — the same transfer, sha256-checked
put agents/sysadmin/gate/agent_exec_gate.py agent_exec_gate.py
# its policy (kd.exec_policy) as the second file, and the deployment's site units from the overlay's
# catalog (critical_units of agents/sysadmin/operations.json); a catalog without them — every unit's stop is critical
put common/kd/exec_policy.py exec_policy.py
# the helper an app.flag plan step runs as the application's account
put agents/sysadmin/gate/agent_app_flag.py agent_app_flag.py
# the helper cron.line, file.drop and gate.script plan steps run as root
put agents/sysadmin/gate/agent_typed_op.py agent_typed_op.py
units_args=""
if git -C "$DEPLOY_TREE" cat-file -e HEAD:agents/sysadmin/operations.json 2>/dev/null; then
  units=$(git -C "$DEPLOY_TREE" show HEAD:agents/sysadmin/operations.json | ${PYTHON:-python} -c "import json, sys; d = json.loads(sys.stdin.buffer.read().decode('utf-8')); u = d.get('critical_units'); assert u is None or (isinstance(u, list) and all(isinstance(x, str) for x in u)); print('-' if u is None else ' '.join(u))") \
    || { echo "the overlay's operations.json: critical_units must be a list of unit patterns" >&2; exit 1; }
  if [ "$units" != - ]; then
    [[ -z $units || $units =~ ^[A-Za-z0-9*][A-Za-z0-9@._*-]{0,120}( [A-Za-z0-9*][A-Za-z0-9@._*-]{0,120})*$ ]] \
      || { echo "the overlay's operations.json: critical_units must be unit patterns like app-*" >&2; exit 1; }
    units_args="--critical-units '$units'"
  fi
fi
# the list of test environments of this server — the catalog's entries marked test, whole. The exec gate
# runs their steps without the owner's signature, so the list goes only from this machine, and never to production.
# An overlay without a catalog has no test environments: the empty list takes an installed one away
envs='{"schema_version": 1, "envs": {}}'
PROD=$(host_value prod_alias) || { echo "could not read prod_alias of the host values" >&2; exit 1; }
if git -C "$DEPLOY_TREE" cat-file -e HEAD:agents/sysadmin/operations.json 2>/dev/null; then
  # the gate reads the catalog with its own, looser rules: first it must be a catalog the cabinet loads
  git -C "$DEPLOY_TREE" show HEAD:agents/sysadmin/operations.json | ${PYTHON:-python} -I -c "
import json, sys
sys.path.insert(0, 'common')
from kd import admin_operations as ops
try:
    ops.Catalog.from_dict(json.loads(sys.stdin.buffer.read().decode('utf-8')), protected=(sys.argv[1],))
except (ops.CatalogError, ValueError) as exc:
    sys.exit(str(exc))
" "$PROD" || { echo "the overlay's operations.json is not a catalog the cabinet loads: nothing is installed" >&2; exit 1; }
  envs=$(git -C "$DEPLOY_TREE" show HEAD:agents/sysadmin/operations.json | ${PYTHON:-python} -I agents/sysadmin/gate/agent_exec_gate.py --test-envs "$target") \
    || { echo "the overlay's operations.json: the test environments of $target are malformed" >&2; exit 1; }
fi
names=$(printf '%s\n' "$envs" | ${PYTHON:-python} -c "import json, sys; print(' '.join(sorted(json.loads(sys.stdin.buffer.read().decode('utf-8'))['envs'])))")
if [ -n "$names" ] && [ "$target" = "$PROD" ]; then
  echo "the overlay's operations.json marks environments of the production server $target as test ($names): refused" >&2
  exit 1
fi
printf '%s\n' "$envs" | ssh "$target" "$sudo sh -c 'cat > $remote_dir/test_envs.json'"
want=$(printf '%s\n' "$envs" | sha256sum | cut -d' ' -f1)
ssh "$target" "$sudo sha256sum $remote_dir/test_envs.json" | grep -q "^$want " \
  || { echo "sha256 of test_envs.json does not match: the transfer is corrupted" >&2; exit 1; }
envs_args="--test-envs $remote_dir/test_envs.json"
echo "test environments of $target: ${names:-none} — their catalog steps run without the owner's signature"
printf '%s\n' "$signers" | ssh "$target" "$sudo sh -c 'cat > $remote_dir/exec_signers.json'"
want=$(printf '%s\n' "$signers" | sha256sum | cut -d' ' -f1)
ssh "$target" "$sudo sha256sum $remote_dir/exec_signers.json" | grep -q "^$want " \
  || { echo "sha256 of exec_signers.json does not match: the transfer is corrupted" >&2; exit 1; }
exec_args="--exec-gate $remote_dir/agent_exec_gate.py --exec-policy $remote_dir/exec_policy.py $units_args --exec-signers $remote_dir/exec_signers.json --alias $target --rp-id $rp_id --origin $origin${exec_mode:+ --exec-mode $exec_mode}${replace_signers:+ --replace-signers}"
if monitor_window begin; then expect_open=1; else echo "warning: the monitor's rollout window did not open; the changes on $target will raise sec alerts" >&2; fi
ssh "$target" "$sudo bash $remote_dir/target_setup.sh --gate $remote_dir/agent_ro_gate.py --snapshot $remote_dir/agent_sec_snapshot.py --usage $remote_dir/agent_hindsight_usage.py $forbidden_args --exec-user $exec_user --stage-ip $STAGE_IP --stage-alias $STAGE --timezone $TIMEZONE $exec_args --app-flag $remote_dir/agent_app_flag.py --typed-op $remote_dir/agent_typed_op.py $envs_args --ro-key '$ro_key' --exec-key '$exec_key' $mysql_args $data_args"
if [ "$expect_open" = 1 ]; then
  expect_open=0
  monitor_window end || echo "warning: the monitor's rollout window did not close; it expires by itself, the changes on $target will raise sec alerts" >&2
fi

# The host key comes from the owner's known_hosts (already trusted); on the factory host it goes to the sysadmin
# and the exec user, without TOFU
found=$(ssh-keygen -F "$host_ip" || true)
keys=$(printf '%s\n' "$found" | grep -v '^#' | awk -v h="$host_ip" 'NF >= 3 && $1 !~ /^@/ {print h, $2, $3}' | sort -u || true)
[ -n "$keys" ] || { echo "the owner's ~/.ssh/known_hosts has no key of $host_ip: log in to $target by hand first" >&2; exit 1; }
# The script for the factory host goes through base64 (stdin is taken by the keys). The file of every user (the
# sysadmin, the exec user, kd-data's user) is written in that user's name, atomically: root never writes into a home
# the user can reshape (a .ssh swapped for a link would send root's write into a system directory).
read -r -d '' remote <<'EOF' || true
set -e
host_ip=$1 target=$2 user=$3 exec_user=$4 data_user=${5:-}
t=$(mktemp)
trap 'rm -f "$t"' EXIT
cat > "$t"
chmod 0644 "$t"
pin_key() {  # <user>: drop the old line of this host, add the pinned keys
  runuser -u "$1" -- sh -c 'set -e; umask 022
  d=/home/$3/.ssh; f=$d/known_hosts
  new=$(mktemp "$d/.known_hosts.XXXXXX")
  trap "rm -f \"\$new\"" EXIT
  { [ ! -f "$f" ] || awk -v h="$2" "\$1 != h" "$f"; cat "$1"; } > "$new"
  chmod 0644 "$new"
  mv -f "$new" "$f"' _ "$t" "$host_ip" "$1"
}
pin_key "$user"
pin_key "$exec_user"
if [ -n "$data_user" ]; then pin_key "$data_user"; fi  # kd-data's user, the same pinned key
touch /etc/digipaddock/servers.enabled
if [ -s /etc/digipaddock/servers.enabled ] && [ -n "$(tail -c1 /etc/digipaddock/servers.enabled)" ]; then echo >> /etc/digipaddock/servers.enabled; fi
grep -qx "$target" /etc/digipaddock/servers.enabled || echo "$target" >> /etc/digipaddock/servers.enabled
EOF
b64=$(printf '%s\n' "$remote" | base64 | tr -d '\n')
printf '%s\n' "$keys" | ssh "$STAGE" "bash -c \"\$(echo $b64 | base64 -d)\" _ $host_ip $target $SYSADMIN_USER $STAGE_EXEC_USER $STAGE_DATA_USER"
echo "done: $target connected (host key pinned, alias in /etc/digipaddock/servers.enabled on the stage)"
