# shellcheck shell=bash
# hindsight_client_for <user> — the Hindsight memory of an agent's user: the Claude Code plugin and
# ~/.hindsight/coding-agent.json with the key. As root; everything in the user's home — on its behalf, atomically.
# Returns 0/1 and does not exit: called by stage_setup.sh (for every user from agents.json) and add_user.sh.
# No /etc/hindsight/coding-agent.env (the Hindsight server is not on the factory host yet) — skipped, code 0. REPO
# comes from the calling script.
hindsight_client_for() {
  local u=${1:?usage: hindsight_client_for <user>} key
  local base HINDSIGHT_PLUGIN_VERSION
  base=$(kd_user base) || return 1  # naming.sh — from the calling script
  [[ $u =~ ^$base(-[a-z][a-z0-9-]{0,22})?$ ]] || { echo "hindsight_client_for: the user must be $base or $base-<agent>: $u" >&2; return 1; }
  [ -f /etc/hindsight/coding-agent.env ] || return 0
  # the plugin's version — from the version catalog (common/install/versions.json)
  HINDSIGHT_PLUGIN_VERSION=$(/usr/bin/python3 -I "$REPO/common/kd/install_catalog.py" version hindsight_plugin) || return 1
  command -v node >/dev/null || { echo "Node 22 (/usr/bin/node) is required for the Hindsight plugin" >&2; return 1; }
  # || true: without a key line grep would return 1 and set -eo pipefail would cut the script off silently, past the error below
  key=$(grep '^HINDSIGHT_API_TENANT_API_KEY=' /etc/hindsight/coding-agent.env | cut -d= -f2- || true)
  [ -n "$key" ] || { echo "no HINDSIGHT_API_TENANT_API_KEY in /etc/hindsight/coding-agent.env" >&2; return 1; }
  if ! runuser -u "$u" -- bash -c 'cat ~/.hindsight/coding-agents/package.json 2>/dev/null' | grep -q "\"version\": \"$HINDSIGHT_PLUGIN_VERSION\""; then
    # npx not from /tmp (a foreign node_modules of www-data/runner); claude in PATH — for `claude mcp add --scope user`;
    # the parameters — through env, the body — in single quotes, the home — $HOME (sudo -H)
    sudo -u "$u" -H env HINDSIGHT_PLUGIN_VERSION="$HINDSIGHT_PLUGIN_VERSION" bash -o pipefail -c 'd=$(mktemp -d -p "$HOME" .npx-XXXXXX) && cd "$d" && PATH=$HOME/.local/bin:$PATH npx -y @vectorize-io/hindsight-coding-agents@$HINDSIGHT_PLUGIN_VERSION install claude-code --server self-hosted --api-url http://127.0.0.1:9077; rc=$?; cd /; rm -rf "$d"; exit $rc' \
      || echo "warning: the Hindsight plugin for $u did not install (network/npm) — retry later" >&2
  fi
  # the whole config from the reference file + the key; the key — in the environment, not in argv; the user itself writes, atomically, 0600
  # the host's banks (memory_banks in host/host.json) — before the user writes the file
  local config
  config=$(/usr/bin/python3 -I - "$REPO/common/kd" "$REPO/common/hindsight/coding-agent.json" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
import naming
print(json.dumps(naming.hindsight_config(json.load(open(sys.argv[2], encoding="utf-8")), naming.load_host()["memory_banks"])))
PY
) || { echo "hindsight_client_for: host/host.json is unreadable" >&2; return 1; }
  KD_KEY="$key" KD_CONFIG="$config" runuser -u "$u" -- python3 -I - <<'PY' || return 1
import json, os, sys, tempfile
src = json.loads(os.environ["KD_CONFIG"])
src["apiToken"] = os.environ["KD_KEY"]
dst = os.path.expanduser("~/.hindsight/coding-agent.json")
os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
os.chmod(os.path.dirname(dst), 0o700)  # плагин создаёт каталог 0775; makedirs не меняет права существующего
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".coding-agent-")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(src, f, ensure_ascii=False, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, dst)
PY
}
