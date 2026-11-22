#!/usr/bin/env bash
# new_agent.sh: install an agent from its manifest agents/<agent>/agent.json (BOSS spec 5.4).
#   new_agent.sh --check <agent>   check manifests, policy, dependencies and conflicts; changes nothing
#   new_agent.sh --plan <agent>    print every system action of the install; changes nothing
#   new_agent.sh --install <agent>  apply and verify; root only; 3 — a module setup stopped at the owner's step
#     (a key to add in the forge, printed above), rolled back like any failure: run it again after the step
#   new_agent.sh --disable <agent>  stop and revoke, preserving home, auth and secrets
#   new_agent.sh --verify <agent>   is the installed agent what this code installs? 0 yes, 3 no; root only, changes
# nothing (the factory's reinstall skips a role that matches)
# Installation decisions and rollback live in common/kd/install_apply.py.
set -euo pipefail

usage() {
  echo "usage: new_agent.sh --check <agent> | --plan <agent> | --install <agent> | --disable <agent> | --verify <agent>" >&2
  exit 2
}

case ${1-} in
  --check | --plan | --disable | --install | --verify) [ $# -eq 2 ] || usage ;;
  *) usage ;;
esac

# The package lives next to this script; -I ignores PYTHON* variables, user site and cwd.
# CDPATH unset and cd silenced: neither may change or pollute the path taken here.
common=$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/.." >/dev/null && pwd -P)
if [ "$1" = --check ] || [ "$1" = --plan ]; then
  exec /usr/bin/python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from kd.install_plan import main; sys.exit(main(sys.argv[1:]))' \
    "$common" --root / "$1" "$2"
fi
# Units, files and users written by the install are ours: the monitor takes them into its baseline.
if [ "$1" != --verify ] && [ -z "${KD_EXPECT_OPEN-}" ] && [ "$(id -u)" = 0 ]; then
  # shellcheck source=lib/expect.sh
  . "$common/install/lib/expect.sh"
  kd_expect_run "new_agent.sh $1 $2 (REVISION $(cat "$common/../REVISION" 2>/dev/null || echo '?'))" bash "${BASH_SOURCE[0]}" "$@"
  exit $?
fi
exec /usr/bin/python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from kd.install_apply import main; sys.exit(main(sys.argv[1:]))' \
  "$common" "$@"
