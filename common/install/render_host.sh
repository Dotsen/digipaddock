#!/usr/bin/env bash
# Install every host file that carries naming tokens (@base@), rendered with this host's user names: the
# systemd units of common/, the units installed earlier by hand or by a module (Hindsight, kd-monitor, the repo sync,
# the cabinet of cabinet_setup.sh) when they are already there, and the managed floor of Claude Code. Run as root by
# stage_setup.sh (step 9) and by kd-release after every switch. Idempotent; enables and starts nothing, only
# daemon-reload.
# usage: bash /opt/digipaddock/common/install/render_host.sh
set -euo pipefail
REPO=/opt/digipaddock
SYSTEMD=/etc/systemd/system
# digipaddock-release.path is only installed here: stage_setup.sh turns it on (releases are the host's);
# digipaddock-board-watch.timer and digipaddock-board-boss-digest.timer as well: the owner turns them on at the BOSS stage check (B08).
# digipaddock-board-grants.path and digipaddock-board-deliver.timer: cabinet_setup.sh turns them on (the owner's decisions reach BOSS).
# digipaddock-mirror.timer: mirror_setup.sh turns it on once the GitHub key is there.
UNITS="digipaddock-alert@.service digipaddock-usage.service digipaddock-usage.timer digipaddock-board-sync.path digipaddock-board-sync.service digipaddock-board-sync.timer digipaddock-board-watch.service digipaddock-board-watch.timer digipaddock-board-boss-digest.service digipaddock-board-boss-digest.timer digipaddock-release.path digipaddock-release.service digipaddock-board-grants.path digipaddock-board-deliver.service digipaddock-board-deliver.timer digipaddock-mirror.service digipaddock-mirror.timer"
# Installed by someone else; re-rendered only where present, never added.
PRESENT="common/systemd/digipaddock-openai-bridge.service common/systemd/digipaddock-model-keys.socket common/systemd/digipaddock-model-keys.service common/hindsight/hindsight-api.service common/hindsight/hindsight-worker.service common/hindsight/hindsight-backup.timer common/hindsight/hindsight-backup.service agents/sysadmin/systemd/digipaddock-monitor.service common/systemd/digipaddock-repo-sync.service common/systemd/digipaddock-repo-sync.timer common/systemd/digipaddock-cabinet.socket common/systemd/digipaddock-cabinet-http.socket common/systemd/digipaddock-cabinet.service common/systemd/digipaddock-cabinet-push.service common/systemd/digipaddock-cabinet-export.service common/systemd/digipaddock-cabinet-export.timer common/systemd/digipaddock-session-host.socket common/systemd/digipaddock-session-host.service common/systemd/digipaddock-review.socket common/systemd/digipaddock-review.service common/systemd/digipaddock-transcribe.socket common/systemd/digipaddock-transcribe@.service"

[ $# -eq 0 ] || { echo "usage: render_host.sh" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
cd /
# shellcheck source=lib/naming.sh
. "$REPO/common/install/lib/naming.sh"
# The units rendered here are ours: the monitor takes them into its baseline.
if [ -z "${KD_EXPECT_OPEN-}" ]; then
  # shellcheck source=lib/expect.sh
  . "$REPO/common/install/lib/expect.sh"
  kd_expect_run "render_host.sh (REVISION $(cat "$REPO/REVISION" 2>/dev/null || echo '?'))" bash "$REPO/common/install/render_host.sh"
  exit $?
fi

for unit in $UNITS; do
  kd_install_rendered "$REPO/common/systemd/$unit" "$SYSTEMD/$unit" 0644
done
for src in $PRESENT; do
  unit=${src##*/}
  [ ! -e "$SYSTEMD/$unit" ] || kd_install_rendered "$REPO/$src" "$SYSTEMD/$unit" 0644
done
# The floor of every agent's rules (root): neither an agent nor a swapped project folder can weaken it.
install -d -o root -g root -m 0755 /etc/claude-code
kd_install_rendered "$REPO/common/claude/managed-settings.template.json" /etc/claude-code/managed-settings.json 0644
systemctl daemon-reload
