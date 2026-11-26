# The factory's host: `kd-diag` targets, the owner's kill switches

Read this before a `kd-diag check`, and when the owner asks what stops you.

## `kd-diag check`

`cabinet-push` reads the cabinet's push path from its database: `sub` (subscription: person, the push gateway's host, created, `last_ok`, device), `pref` (a person's notification modes and `quiet` hours), the newest 40 `note` (notification: kind, tag, due, sent, result, attempts, tried, title) and `delivery` (per device: sent, result, attempts, tried) — one line `diag <target>: N rows at <time> (checked by kd-diag for @sysadmin)`, then the rows; the card's thread keeps only the count. Every check of a query reads anew; `hindsight-api`/`hindsight-worker` are HTTP health checks (the same target within 10 minutes answers the stored result `(reused)`, another `--key` checks again). At most 10 new checks per card per hour (exit code 5).

## The owner's kill switches

The owner has kill switches on {{host.stage_alias}}: execution — `rm /etc/digipaddock/cap/sysadmin/exec.json` (`kd-admin-run` and `kd-admin-critical` stop working on all servers at once; `…/sysadmin/base.json` — `kd-task`, `kd-work` and `kd-diag`: no board reads or writes, no reports, no service checks), your own commands outside the sandbox — `rm /etc/digipaddock/cap/sysadmin/self.json` (`ssh-ro`, the monitor, `kd-notes`), host reads — `…/sysadmin/host.json` (`kd-host-read`), every agent's channels at once — `systemctl stop digipaddock-cap.target`, reading — `: > /etc/digipaddock/servers.enabled` (`ssh-ro` and `kd-admin-run` accept no server); on a server — its exec gate in `observe` or `enforce` mode (`deploy_target.sh <alias> --exec-mode observe|enforce`) and the owner's signing keys there; on {{host.stage_alias}} — the envelopes flag `/etc/digipaddock/exec-envelopes` (removed: the executors send raw commands, which only `observe` servers run). They are the owner's tools, never yours to run or to propose.
