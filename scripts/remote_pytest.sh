#!/usr/bin/env bash
# Run selected tests of this working tree on a Linux host over ssh (flock, ACL, O_PATH, symlinks).
# usage: scripts/remote_pytest.sh <ssh-host> [pytest args...]   e.g. ... myhost common/tests/test_attention.py
# REMOTE_PYTHON=<path> picks the host's interpreter (default python3), e.g. a venv with the cabinet's packages.
# Ships tracked and untracked (not ignored) files to a temporary directory, runs pytest there, removes it.
set -euo pipefail
host=${1:?usage: remote_pytest.sh <ssh-host> [pytest args...]}
shift
dir=$(ssh "$host" 'mktemp -d /tmp/kd-pytest.XXXXXX')
trap 'ssh "$host" "rm -rf $dir"' EXIT
git ls-files -co --exclude-standard -z | tar --null -cf - -T - | ssh "$host" "tar -xf - -C $dir"
args=$(printf '%q ' "$@")
py=$(printf '%q' "${REMOTE_PYTHON:-python3}")
ssh "$host" "cd $dir && $py -m pytest -q -p no:cacheprovider -rfs $args"
