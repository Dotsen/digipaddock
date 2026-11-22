#!/usr/bin/env bash
# DigiPaddock: the factory host from an empty Ubuntu 24.04, the installers in order, as root.
# usage: bash <tree>/common/install/bootstrap.sh <phase>|all [--plan] [--overlay <dir> --code <dir>]
# Before the first tree, from a checkout of the code: --overlay and --code name the overlay's and the code's
# checkouts; after it everything comes from /opt/digipaddock. The phases and what each does — common/kd/bootstrap.py.
# Exit 0 all ok, 3 the owner's step (its line says what to do), 1 an installer failed, 2 usage.
set -euo pipefail
TREE=$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." >/dev/null && pwd -P)
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
exec /usr/bin/python3 -I "$TREE/common/kd/bootstrap.py" "$@"
