#!/usr/bin/env bash
# kd-review: the Forgejo approver of the owner's passkey signature in the cabinet, run as root:
#   review_setup.sh install|check|disable
# install: the service user (<prefix>-review, nologin), its token directory /etc/digipaddock/review (0700; the
#   token file <forge_review_user of host/host.json>.env is the owner's step — check names it while it is missing), the signers registry
#   /var/lib/digipaddock/signers (0700, filled by root one key at a time: `kd-review signers add <user>` lists the
#   cabinet's keys, `… --credential <id>` adds one), the link /usr/local/bin/kd-review, the units rendered,
#   digipaddock-review.socket enabled and started; the spent nonces live in the unit's StateDirectory. Idempotent; needs the
# cabinet (its user and venv) installed first. Runs under the monitor's expectation (kd_expect_run).
# check: the same state, changing nothing, then `kd-review check` (token, origin, at least one signer); exit 1 with
#   the reasons. `check installed` leaves out the owner's parts (the token, the signers): what install makes, for
# bootstrap.sh before its secrets and logins.
# disable: the socket is disabled and stopped, the service stopped; the user, the token and the registry stay.
set -euo pipefail
REPO=/opt/digipaddock
UNITS=/etc/systemd/system
TOKEN_DIR=/etc/digipaddock/review
SIGNERS=/var/lib/digipaddock/signers
VENV_PY=/var/lib/digipaddock/venv/cabinet/bin/python
BIN=$REPO/agents/dev/bin/kd-review
USAGE="usage: review_setup.sh install|check [installed]|disable"

mode=${1:-} scope=full
case $mode in
  install|disable) [ $# -eq 1 ] || { echo "$USAGE" >&2; exit 2; } ;;
  check)
    if [ $# -eq 2 ] && [ "$2" = installed ]; then scope=installed; fi
    [ $# -eq 1 ] || [ "$scope" = installed ] || { echo "$USAGE" >&2; exit 2; } ;;
  *) echo "$USAGE" >&2; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
# root's cwd (/root 0700) is closed to runuser of the service user, and python looks for modules in cwd first
cd /
# shellcheck source=../../../common/install/lib/naming.sh
. "$REPO/common/install/lib/naming.sh"
# the host's names of the commands this installer links on PATH
REVIEW_LINK=/usr/local/bin/$(kd_cmd kd-review)
# the token file is named after the Forgejo account
TOKEN=$TOKEN_DIR/$(kd_host forge_review_user).env || { echo "host/host.json is not readable" >&2; exit 1; }
# Everything install changes is ours: the monitor takes it into its baseline.
if [ "$mode" = install ] && [ -z "${KD_EXPECT_OPEN-}" ]; then
  # shellcheck source=../../../common/install/lib/expect.sh
  . "$REPO/common/install/lib/expect.sh"
  kd_expect_run "review_setup.sh $* (REVISION $(cat "$REPO/REVISION" 2>/dev/null || echo '?'))" \
    bash "$REPO/agents/dev/install/review_setup.sh" "$@"
  exit $?
fi
review=$(kd_user review)
cabinet=$(kd_user cabinet)

problems=()
want() {  # want <path> <owner:group> <mode>: a real directory or file with exactly this owner and mode
  local path=$1 owner=$2 perm=$3
  if [ -L "$path" ] || [ ! -e "$path" ]; then problems+=("$path: missing or a symlink"); return; fi
  local have
  have=$(stat -c '%U:%G %a' "$path")
  [ "$have" = "$owner $perm" ] || problems+=("$path: $have, needs $owner $perm")
}

check() {  # check [full|installed]: installed leaves out the owner's steps (the token, the signers)
  local scope=${1:-full}
  if ! id "$review" >/dev/null 2>&1; then
    problems+=("no user $review")
  else
    [ "$(getent passwd "$review" | cut -d: -f7)" = /usr/sbin/nologin ] || problems+=("$review: shell is not nologin")
    want "$TOKEN_DIR" "$review:$review" 700
    want "$SIGNERS" "$review:$review" 700
    runuser -u "$review" -- "$VENV_PY" -I -c "import webauthn" 2>/dev/null \
      || problems+=("$review: $VENV_PY does not import webauthn (cabinet_setup.sh install)")
    if [ -e "$TOKEN" ] || [ "$scope" = full ]; then
      want "$TOKEN" "$review:$review" 600
    fi
  fi
  id "$cabinet" >/dev/null 2>&1 || problems+=("no user $cabinet: the cabinet is not installed")
  [ "$(readlink "$REVIEW_LINK" || true)" = "$BIN" ] || problems+=("$REVIEW_LINK: not a link to the code")
  local unit
  for unit in digipaddock-review.socket digipaddock-review.service; do
    kd_render "$REPO/common/systemd/$unit" | cmp -s - "$UNITS/$unit" || problems+=("$UNITS/$unit: differs from the code")
  done
  systemctl is-enabled --quiet digipaddock-review.socket || problems+=("digipaddock-review.socket is disabled")
  systemctl is-active --quiet digipaddock-review.socket || problems+=("digipaddock-review.socket is not running")
  if [ ${#problems[@]} -eq 0 ] && [ "$scope" = full ]; then
    runuser -u "$review" -- "$VENV_PY" -I "$BIN" check || problems+=("kd-review check: see the lines above")
  fi
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'kd-review: %s\n' "${problems[@]}" >&2
    exit 1
  fi
}

install_all() {
  [ -x "$VENV_PY" ] || { echo "no $VENV_PY: run cabinet_setup.sh install first" >&2; exit 1; }
  id "$cabinet" >/dev/null 2>&1 || { echo "no user $cabinet: run cabinet_setup.sh install first" >&2; exit 1; }
  if ! id "$review" >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$review"
  fi
  install -d -o "$review" -g "$review" -m 0700 "$TOKEN_DIR"
  install -d -o "$review" -g "$review" -m 0700 "$SIGNERS"
  ln -sfn "$BIN" "$REVIEW_LINK"
  local unit
  for unit in digipaddock-review.socket digipaddock-review.service; do
    kd_install_rendered "$REPO/common/systemd/$unit" "$UNITS/$unit" 0644
  done
  systemctl daemon-reload
  systemctl enable --now digipaddock-review.socket
  # a running service keeps the old code: restart it, the next connection starts the new one
  systemctl try-restart digipaddock-review.service
}

case $mode in
  install)
    install_all
    check installed
    [ -e "$TOKEN" ] || echo "kd-review: next, the Forgejo token in $TOKEN ($review 0600), then kd-review signers add <user> (the list of keys), kd-review signers add <user> --credential <id> and review_setup.sh check"
    ;;
  check) check "$scope" ;;
  disable)
    if systemctl cat digipaddock-review.socket >/dev/null 2>&1; then
      systemctl disable --now digipaddock-review.socket
      systemctl stop digipaddock-review.service
    fi ;;
esac
