#!/usr/bin/env bash
# pipeline_setup.sh: the pipeline's mode — observe or enforce — in /etc/digipaddock/pipeline.json;
# root only.
#   status    the cabinet's view: the mode, since when, the outcomes, waits, problems and continuations
#   check     ready for enforce: the cabinet speaks protocol 1; every agent on the session host has its installed
#             texts current (new_agent.sh --check: the ones with kd-work finish); kd-work knows finish. Exit 1 with
#             a reason per agent; nothing is written
#   enable    check, then enforce: the cabinet queues continuations, asks BOSS about problems, holds Done to policy
#   disable   observe: outcomes, waits and problems are still recorded; nothing new starts; the queue stays
# No agent writes the file; the cabinet re-reads it when it changes.
set -euo pipefail
REPO=/opt/digipaddock
USAGE="usage: pipeline_setup.sh status | check | enable | disable"

mode=${1-}
case $mode in
  status | check | enable | disable) [ $# -eq 1 ] || { echo "$USAGE" >&2; exit 2; } ;;
  *) echo "$USAGE" >&2; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
cd /

pipeline() {  # write observe|enforce | show — kd.pipeline_config, written whole or not at all
  /usr/bin/python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from kd.pipeline_config import main; sys.exit(main(sys.argv[1:]))' \
    "$REPO/common" "$@"
}

hosted() {  # the agents on the session host, one per line
  /usr/bin/python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from kd.session_config import main; sys.exit(main(sys.argv[1:]))' \
    "$REPO/common" list | cut -d' ' -f1
}

problems=()
check() {
  local status agent agents
  if status=$("$REPO/common/bin/kd-cabinet" pipeline status 2>&1); then
    grep -qx 'protocol: 1' <<<"$status" || problems+=("cabinet: not protocol 1 (deploy the code and cabinet_setup.sh install)")
  else
    problems+=("the cabinet does not answer pipeline status: ${status##*$'\n'}")
  fi
  # root's kd-work would re-exec through kd-cap; the deployed text is what the agents run
  grep -q 'kd-work finish KD-' "$REPO/common/bin/kd-work" || problems+=("kd-work does not know finish (deploy the code)")
  if ! agents=$(hosted); then
    problems+=("the host's agent list is not readable (session_host_setup.sh check)")
  fi
  for agent in $agents; do
    "$REPO/common/install/new_agent.sh" --check "$agent" >/dev/null 2>&1 \
      || problems+=("$agent: the installed texts differ (new_agent.sh --install $agent)")
  done
}

report() {
  if [ ${#problems[@]} -eq 0 ]; then
    echo "pipeline: ready for enforce"
    return 0
  fi
  printf 'pipeline: %s\n' "${problems[@]}" >&2
  return 1
}

case $mode in
  status)
    exec "$REPO/common/bin/kd-cabinet" pipeline status
    ;;
  check)
    check
    report
    ;;
  enable)
    check
    report
    pipeline write enforce
    echo "pipeline: enforce"
    ;;
  disable)
    pipeline write observe
    echo "pipeline: observe (the data and the queue stay)"
    ;;
esac
