# Linux user names of this host (common/kd/naming.py). Source it; every function fails closed.
#   kd_user base|exec|forge|repo|cabinet|push|review|transcribe|observe|data|ci|agent <name>   print one user name
#   kd_render <file>                       print <file> with naming tokens resolved
# kd_host <key>                          print one deployment value of host/host.json
# kd_cmd kd-<word>                       print the host's name of a command the repository spells kd-<word>
#   kd_names                               print "<prefix> <commands> <cards>" of this host
#   kd_install_rendered <src> <dst> <mode> render and install root:root
#   kd_factory_host                        0 — this is the factory host: stage_ip of host.json on an interface, or
#                                          behind NAT (host/install.json behind_nat) cabinet_host resolving to it
KD_NAMING_PY=$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." >/dev/null && pwd -P)/kd/naming.py
kd_user() { /usr/bin/python3 -I "$KD_NAMING_PY" user "$@"; }
kd_render() { /usr/bin/python3 -I "$KD_NAMING_PY" render "$1"; }
kd_host() { /usr/bin/python3 -I "$KD_NAMING_PY" host "$1"; }
kd_cmd() { /usr/bin/python3 -I "$KD_NAMING_PY" cmd "$1"; }
kd_names() { /usr/bin/python3 -I "$KD_NAMING_PY" names; }
kd_factory_host() {
  local ip nat
  ip=$(kd_host stage_ip) || { echo "host/host.json is unreadable" >&2; return 1; }
  hostname -I | tr ' ' '\n' | grep -qxF "$ip" && return 0
  nat=$(/usr/bin/python3 -I -c 'import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("behind_nat") is True)
except (OSError, ValueError, AttributeError):
    print(False)' "${KD_NAMING_PY%/common/kd/naming.py}/host/install.json")
  if [ "$nat" = True ] && getent ahosts "$(kd_host cabinet_host)" | awk '{print $1}' | grep -qxF "$ip"; then
    return 0
  fi
  echo "this is not the factory host ($ip)" >&2
  return 1
}
kd_install_rendered() {
  local tmp rc=0
  tmp=$(mktemp) || return 1
  { kd_render "$1" > "$tmp" && install -o root -g root -m "$3" "$tmp" "$2"; } || rc=1
  rm -f "$tmp"
  return $rc
}
