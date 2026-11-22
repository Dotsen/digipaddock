#!/usr/bin/env bash
# kd-transcribe: the one-shot voice transcription executor, run as root:
#   transcribe_setup.sh install|check|disable|lock
# install: the service user (<prefix>-transcribe, nologin), its venv /var/lib/digipaddock/venv/transcribe from the
#   hash lock common/transcribe/requirements.txt (wheels only; refuses a lock without hashes), the model (medium,
#   owner's decision 22.10.2026) downloaded once into /var/lib/digipaddock/transcribe/models (root:<user> 0750,
#   read only for the executor), the units rendered, digipaddock-transcribe.socket enabled and started. Network: PyPI and
#   huggingface.co during install only; the executor itself has none. Idempotent; needs the cabinet's user (the
#   socket's group).
# check: the same state, changing nothing: the venv matches the lock, the user imports faster_whisper and loads the
#   model offline, the units are as in the code, the socket runs, and a probe through the socket as the cabinet's
#   user comes back «probe_ok» (the unit loaded the model and decoded a second of silence in its own sandbox).
#   Exit 1 with the reasons.
# disable: the socket stops and is disabled, running instances stop; the user, venv and model stay.
# lock: (network) builds the hash lock for requirements.in in a scratch venv and prints it — the content of
#   common/transcribe/requirements.txt, committed in a PR; nothing on the host changes.
set -euo pipefail
REPO=/opt/digipaddock
UNITS=/etc/systemd/system
VENV=/var/lib/digipaddock/venv/transcribe
MODELS=/var/lib/digipaddock/transcribe/models
MODEL=medium
REQ_IN=$REPO/common/transcribe/requirements.in
REQ=$REPO/common/transcribe/requirements.txt
CLIENT=$REPO/common/kd/transcribe_client.py
USAGE="usage: transcribe_setup.sh install|check|disable|lock"

mode=${1:-}
case $mode in
  install|check|disable|lock) ;;
  *) echo "$USAGE" >&2; exit 2 ;;
esac
[ $# -eq 1 ] || { echo "$USAGE" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
# root's cwd (/root 0700) is closed to runuser of the service user, and python looks for modules in cwd first
cd /
# shellcheck source=lib/naming.sh
. "$REPO/common/install/lib/naming.sh"
transcribe=$(kd_user transcribe)
cabinet=$(kd_user cabinet)

lock_sha() { sha256sum "$REQ" | cut -d' ' -f1; }

load_model() {  # load_model <user>: the model loads offline from $MODELS (as the executor would)
  runuser -u "$1" -- env HF_HUB_OFFLINE=1 HOME=/nonexistent "$VENV/bin/python" -I -c \
    'import sys; from faster_whisper import WhisperModel; WhisperModel(sys.argv[1], device="cpu", compute_type="int8", download_root=sys.argv[2], local_files_only=True)' \
    "$MODEL" "$MODELS"
}

problems=()
check() {
  if ! id "$transcribe" >/dev/null 2>&1; then
    problems+=("no user $transcribe")
  else
    [ "$(getent passwd "$transcribe" | cut -d: -f7)" = /usr/sbin/nologin ] || problems+=("$transcribe: shell is not nologin")
    if [ ! -x "$VENV/bin/python" ]; then
      problems+=("no $VENV")
    else
      [ "$(cat "$VENV/.requirements.sha256" 2>/dev/null || true)" = "$(lock_sha)" ] \
        || problems+=("$VENV: does not match the lock $REQ (transcribe_setup.sh install)")
      load_model "$transcribe" 2>/dev/null || problems+=("$transcribe: model $MODEL does not load from $MODELS offline")
    fi
    [ "$(stat -c '%U:%G %a' "$MODELS" 2>/dev/null || true)" = "root:$transcribe 750" ] \
      || problems+=("$MODELS: must be root:$transcribe 750")
  fi
  id "$cabinet" >/dev/null 2>&1 || problems+=("no user $cabinet — the cabinet is not installed")
  local unit
  for unit in digipaddock-transcribe.socket digipaddock-transcribe@.service; do
    kd_render "$REPO/common/systemd/$unit" | cmp -s - "$UNITS/$unit" || problems+=("$UNITS/$unit: differs from the code")
  done
  systemctl is-enabled --quiet digipaddock-transcribe.socket || problems+=("digipaddock-transcribe.socket is disabled")
  systemctl is-active --quiet digipaddock-transcribe.socket || problems+=("digipaddock-transcribe.socket is not running")
  if [ ${#problems[@]} -eq 0 ]; then
    # as the real caller (the socket's group and mode), the model loaded inside the unit's own sandbox
    runuser -u "$cabinet" -- python3 -I "$CLIENT" smoke \
      || problems+=("the check through the socket failed: see the lines above and journalctl -u 'digipaddock-transcribe@*'")
  fi
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'kd-transcribe: %s\n' "${problems[@]}" >&2
    exit 1
  fi
  echo "kd-transcribe: ok"
}

install_all() {
  grep -q -- '--hash=sha256:' "$REQ" || { echo "$REQ has no hashes — first transcribe_setup.sh lock and a PR with the lock" >&2; exit 1; }
  id "$cabinet" >/dev/null 2>&1 || { echo "no user $cabinet — run cabinet_setup.sh install first" >&2; exit 1; }
  if ! id "$transcribe" >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$transcribe"
  fi
  if [ "$(cat "$VENV/.requirements.sha256" 2>/dev/null || true)" != "$(lock_sha)" ]; then
    rm -rf "$VENV"  # a new lock builds a fresh venv: nothing left over from the old one
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --disable-pip-version-check --no-input -q --require-hashes --only-binary=:all: -r "$REQ"
    lock_sha > "$VENV/.requirements.sha256"
  fi
  install -d -o root -g root -m 0755 "$(dirname "$MODELS")"
  install -d -o root -g "$transcribe" -m 0750 "$MODELS"
  if ! load_model root 2>/dev/null; then  # once, with network: huggingface.co
    HOME=/root "$VENV/bin/python" -I -c \
      'import sys; from faster_whisper import WhisperModel; WhisperModel(sys.argv[1], device="cpu", compute_type="int8", download_root=sys.argv[2])' \
      "$MODEL" "$MODELS"
  fi
  chown -R "root:$transcribe" "$MODELS"
  chmod -R u=rwX,g=rX,o= "$MODELS"
  local unit
  for unit in digipaddock-transcribe.socket digipaddock-transcribe@.service; do
    kd_install_rendered "$REPO/common/systemd/$unit" "$UNITS/$unit" 0644
  done
  systemctl daemon-reload
  systemctl enable digipaddock-transcribe.socket
  # a running socket keeps its old settings: restart it (the connections in flight keep their own processes)
  systemctl restart digipaddock-transcribe.socket
}

build_lock() {
  local tmp
  tmp=$(mktemp -d)
  # expanded now: the EXIT trap runs after the function, when the local is gone
  trap "rm -rf '$tmp'" EXIT
  python3 -m venv "$tmp/venv" >&2
  "$tmp/venv/bin/pip" install --disable-pip-version-check --no-input -q --only-binary=:all: -r "$REQ_IN" >&2
  # --all: a dependency on setuptools or wheel must be locked too (plain freeze hides them); pip itself is the venv's
  "$tmp/venv/bin/pip" freeze --all --disable-pip-version-check | grep -v -i '^pip==' | sort > "$tmp/frozen.txt"
  # the wheels of both machines a factory host runs on (an aarch64 host found x86_64 hashes only), for this
  # Python; glibc from manylinux2014 up to Ubuntu 24.04's. pip install keeps the candidate whose hash is locked.
  local py arch glibc platforms
  py=$("$tmp/venv/bin/python" -I -c 'import sys; print("%d%d" % sys.version_info[:2])')
  for arch in x86_64 aarch64; do
    platforms=(--platform "manylinux2014_$arch")
    for glibc in $(seq 17 39); do platforms+=(--platform "manylinux_2_${glibc}_$arch"); done
    "$tmp/venv/bin/pip" download --disable-pip-version-check --no-input -q --no-deps --only-binary=:all: \
      --python-version "$py" --implementation cp --abi "cp$py" "${platforms[@]}" -d "$tmp/wheels" -r "$tmp/frozen.txt" >&2
  done
  echo "# The transcription venv's lock (KD-9075): transcribe_setup.sh lock, $(date -u +%F), $(python3 -V), $(uname -m)."
  echo "# From common/transcribe/requirements.in; wheels for x86_64 and aarch64 Linux."
  "$tmp/venv/bin/python" -I "$REPO/common/kd/transcribe_lock.py" "$tmp/frozen.txt" "$tmp/wheels"
}

case $mode in
  install)
    install_all
    check
    ;;
  check) check ;;
  disable)
    if systemctl cat digipaddock-transcribe.socket >/dev/null 2>&1; then
      systemctl disable --now digipaddock-transcribe.socket
    fi
    systemctl stop 'digipaddock-transcribe@*.service' 2>/dev/null || true
    ;;
  lock) build_lock ;;
esac
