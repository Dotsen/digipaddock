#!/usr/bin/env bash
# DigiPaddock: nginx for the cabinet host (cabinet_host of host/host.json) on the factory host — the owner
# cabinet (its web through the
# cabinet's http socket, its built frontend, the security headers). Idempotent, root; run by cabinet_setup.sh install.
# usage: bash /opt/digipaddock/common/install/board_setup.sh
set -euo pipefail
REPO=/opt/digipaddock
# shellcheck source=lib/naming.sh
. "$REPO/common/install/lib/naming.sh"
# the site's host name and the factory host's address — host/host.json of the release tree
HOST=$(kd_host cabinet_host) || { echo "host/host.json is unreadable" >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
kd_factory_host || exit 1  # behind NAT — by the cabinet's DNS

install -d -o root -g root -m 0755 /var/www/$HOST
# the cabinet's logs in a directory of their own (only they: the host reader's default ACL there is no
# wider than them), rotated like nginx's own; before nginx -t, which opens them
install -d -o root -g adm -m 0755 /var/log/nginx/board
install -o root -g root -m 0644 "$REPO/common/logrotate/digipaddock-board-nginx" /etc/logrotate.d/digipaddock-board-nginx
install -o root -g root -m 0644 "$REPO/common/nginx/digipaddock-board-limit.conf" /etc/nginx/conf.d/digipaddock-board-limit.conf
# included by the site: before it, or nginx -t fails on the include
install -o root -g root -m 0644 "$REPO/common/nginx/digipaddock-cabinet-proxy.conf" /etc/nginx/digipaddock-cabinet-proxy.conf
kd_install_rendered "$REPO/common/nginx/digipaddock-cabinet-headers.conf" /etc/nginx/digipaddock-cabinet-headers.conf 0644

conf=/etc/nginx/sites-available/$HOST.conf
if [ -f /etc/letsencrypt/live/$HOST/fullchain.pem ]; then
  kd_install_rendered "$REPO/common/nginx/cabinet.conf" "$conf" 0644
else
  # only the HTTP block: there is no certificate yet
  kd_render "$REPO/common/nginx/cabinet.conf" | sed '/^# --- https ---$/,/^# --- \/https ---$/d' > "$conf"
  chmod 0644 "$conf"
fi
ln -sfn "$conf" /etc/nginx/sites-enabled/$HOST.conf
# a broken config must not stay enabled: otherwise the next nginx reload would take all sites down
nginx -t || { rm -f /etc/nginx/sites-enabled/$HOST.conf; exit 1; }
systemctl reload nginx

if [ ! -f /etc/letsencrypt/live/$HOST/fullchain.pem ]; then
  certbot certonly --webroot -w "/var/www/$HOST" -d "$HOST" --non-interactive --keep --deploy-hook 'systemctl reload nginx'
  kd_install_rendered "$REPO/common/nginx/cabinet.conf" "$conf" 0644
  nginx -t || { rm -f /etc/nginx/sites-enabled/$HOST.conf; exit 1; }
  systemctl reload nginx
fi
echo "https://$HOST: the cabinet (sign-in by passkey)"
