#!/usr/bin/env bash
# worker_slots.sh: the worker slot pools of common/worker/worker-slots.json (BOSS spec 5.7, 5.8.6, C03/C05); root only.
#   worker_slots.sh --check     report every difference from the registered layout; changes nothing
#   worker_slots.sh --install   create or repair the slot folders and the base user's result folder; enable the
#                               slots when every pool owner's login is in place
#   worker_slots.sh --disable   empty the enabled list: neither kd-worker nor the session host takes a job;
#                               folders, copies and results stay (running jobs end on their own)
# No agent user, login or sudoers line is made: jobs run as the agent that owns the pool.
set -euo pipefail

usage() {
  echo "usage: worker_slots.sh --check | --install | --disable" >&2
  exit 2
}

case ${1-} in
  --check | --install | --disable) [ $# -eq 1 ] || usage ;;
  *) usage ;;
esac

# The package lives next to this script; -I ignores PYTHON* variables, user site and cwd.
common=$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/.." >/dev/null && pwd -P)
if [ "$1" = --install ]; then
  # The enabled list it writes is a root file the monitor watches: inside its rollout window.
  # shellcheck source=lib/expect.sh
  . "$common/install/lib/expect.sh"
  if [ -z "${KD_EXPECT_OPEN-}" ]; then
    kd_expect_run "worker_slots.sh --install" bash "${BASH_SOURCE[0]}" "$@"
    exit $?
  fi
fi
rc=0
/usr/bin/python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from kd.worker_slots import main; sys.exit(main(sys.argv[1:]))' \
  "$common" "$1" || rc=$?
exit $rc
