## Servers

| Server | Address | Role | Notes |
|---|---|---|---|
| `example-stage` | 203.0.113.10 | Staging, **home of the agents, including you** | **Do not reboot** — only by hand, in a window |
| `example-prod` | 203.0.113.20 | Prod: the application API and its database | Any change — with special care |
| `node-a` | 203.0.113.30 | WireGuard node | Login `ubuntu`; ssh-exec adds sudo itself |
| `node-b` | 203.0.113.31 | Backup mirror `/backups` | Read only |

If one change goes to several servers (OS upgrade, config rollout), example-prod last and only after the owner's explicit consent for prod.

You have no other servers.

- MySQL runs on `example-prod`: read large tables only with a selective WHERE — a full scan chokes prod.

## Application rules

The same rules are in the deployment's `host/sysadmin_rules.json`: the denylist enforces them, this list explains them.

- Application code trees besides `/var/www`: `/opt/app` — it changes only through git and deploy.
- `sync_manager … sync` runs only with `--dry-run`: the owner runs the real sync.
- `shop-a.example` and `shop-b.example` are reached only through the SOCKS proxy: `curl --socks5-hostname 127.0.0.1:1080 …`.
- After an edit of the API's `config.ini`, restart its services (`app-worker@*`).

## Documentation

- The application's documentation: `{{host.docs_dir}}` — read directly, without ssh-ro, with the Read and Grep tools; in Bash write absolute paths.

## Prod OS upgrade procedure (example-prod)

0. `apt-get update`.
1. Baseline BEFORE: `ip rule`, `wg show`, the application's health check, listeners (`ss -tlnpu`). Write it to `memory/notes.md`.
2. `full-upgrade` as a transient unit: `systemd-run --unit=os-upgrade --setenv=NEEDRESTART_SUSPEND=1 --setenv=DEBIAN_FRONTEND=noninteractive apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade`. `DEBIAN_FRONTEND` covers only debconf; a dpkg question about a config file with stdin=/dev/null would fail and leave packages unconfigured, hence `--force-confdef`/`--force-confold` are needed.
3. Wait for the `os-upgrade` unit to finish, check that the unit is no longer running (`systemctl is-active os-upgrade` — `active` means "still running", `inactive`/`failed` — finished), then the result (`systemctl show -p Result,ExecMainStatus os-upgrade`, `journalctl -u os-upgrade`) and that `dpkg --audit` prints nothing (via `ssh-ro`). Only after that propose the reboot. Before a retry after a failed run — `systemctl reset-failed os-upgrade` (otherwise the unit name is taken).
4. `reboot` — a separate critical action (see Tools).
5. Compare with the baseline.

## Known pitfalls

- Restarting `systemd-networkd` wipes the `ip rule` entries from wg-quick. That is why it is forbidden.
