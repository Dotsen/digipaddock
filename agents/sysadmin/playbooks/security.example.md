# Sysadmin security playbook

Read on a monitor event `logins`, `sec:*`, `ports`, `wgpeers`, `updates`, `reboot`, `autoupdates`, `eol`, `posture`, `load`, `health` and for an audit on the owner's word. General rules: look first (`ssh-ro`), then one question or one proposal to the owner; a change — only with the owner's exact approval: «by card» below means a board card whose plan the owner approves in the cabinet, then its `script` steps run with `kd-admin-run`, or for a firewall or WireGuard change, stopping a unit or a reboot a critical action (`kd-admin-propose-action`, then `kd-admin-critical`; see Tools in CLAUDE.md). Name the server explicitly in every message.

## General incident rules

1. **Evidence → isolation → cleanup**, in that order. Do not reboot or kill first: traces get lost.
2. example-prod — any action only after an explicit «да»; example-stage — on a suspected break-in fix nothing and investigate no deeper than one `ssh-ro`; one line to the owner noting that the kill switches are in his hands (`rm /etc/digipaddock/cap/sysadmin/exec.json` stops execution on every server, `systemctl stop digipaddock-cap.target` every channel of every agent, `: > /etc/digipaddock/servers.enabled` reading too) — remind him they exist, do not propose running them. On example-stage do not propose `kill`/`pkill`: the session host, the monitor and your own session live there.
3. Access (keys, `sshd_config`, `authorized_keys`, passwords, users, groups) is changed by the owner by hand — the denylist will not let it through; you report facts and propose what exactly to change.
4. Never block 203.0.113.10 (stage) or the owner's trusted addresses (the IP from a `logins` event is already untrusted, otherwise there would be no alert; the exception is 203.0.113.10: a login not by `agent-ro`); block a source only after the owner answers «чужой».
5. Everything goes into your thread's notes with a date: server, what you saw, what was done, what was accepted; what every later session must know — also `kd-notes add` into the shared `notes.md`. Do not write secrets.
6. `accept` / `baseline` / `trust` are proposals: each opens a `monitor_trust` decision the owner signs in the cabinet, and only then the monitor applies it (accept and trust on its next tick, a baseline within 10 min; the decision shows «применено» or «ошибка: …»). Propose them only on the owner's message in the cabinet, with `--text` — why it is safe to trust, in one line in the owner’s language. Before `baseline` — first trust the owner's addresses (propose `kd-monitor trust`), otherwise his own logins become alerts. `kd-monitor trust --remove` needs no signature, only his word. The line «принято: <сервер>: выкатка «…»» means a root installer (`new_agent.sh`, `render_host.sh`, `stage_setup.sh`, `deploy_target.sh`) took its own changes into the baseline through the rollout window — nothing to do; `kd-monitor expect` is not yours. The steps of your own approved plans keep the baseline the same way (plan windows): the card's thread gets «в эталон: …» and no incidents; what stays an incident — a dangerous change (keys, sudoers, groups, a uid 0 user, a weaker sshd, `ld.so.preload`, a firewall off) or an object that differed from the baseline before the step — is linked to the card: explain it there and propose its accept if it is the step's intended result. Many incidents of one kind — one bundle: `kd-monitor accept P-a..P-b --text "<why>"`, one signature of the owner.
7. `last`/`who` show only interactive sessions (with a pty); `ssh server command` logins and scp are visible only in auth.log/journal.

## Login by an unknown identity (`logins`)

Look: `tail -n 300 /var/log/auth.log` around the time (on node-b and wherever there is no auth.log — `journalctl _COMM=sshd + _COMM=sshd-session -n 300 --no-pager -o short-iso`), `who`, `last -F -i -n 20`, `cat /etc/passwd` (the user's line), `ps -eo user,pid,ppid,etime,cmd` (the user's lines), `ss -tnp` (connections from that IP).
The event names the key when the snapshots know it: «ключ из <authorized_keys этого сервера> «комментарий»» and «лежит на <сервер>: /home/<user>/.ssh/<файл>.pub» — whose key it is; `kd-monitor keys <fingerprint>` shows every place of that key across the servers and where a login with it is already accepted.
Report: server, who/from where/when, what they did (if visible), whose key it is by the snapshots, ask in the owner’s language whether the key is theirs or the other developer’s.
- His own — propose `kd-monitor accept P-<n> --label <метка> --text "<why>"`; if the address is permanent — also `kd-monitor trust <ip> --label <метка>`. He signs both in the cabinet; until then the alert stays.
- The access is already gone (the key taken off, the user deleted) — `accept` would trust a removed key: confirm with `kd-monitor keys <fingerprint>` that no `authorized_keys` of that server allows it any more, then, on the owner's word, propose `kd-monitor revoke P-<n> --text "<how it was removed>"`. After his signature the monitor checks its latest snapshot and closes the P-n as «доступ снят» without the baseline; a key still allowed — «ошибка: ключ ещё разрешён на …», the P-n stays open.
- «чужой» — propose by card blocking the source: `ufw insert 1 deny from <IP>` (only where ufw is active — on a disabled ufw the rule blocks nothing; if ufw has no rules yet, `insert 1` fails — then `ufw deny from <IP>` without a position) or `iptables -I INPUT -s <IP> -j DROP` (for an IPv6 source — `ip6tables`; it does not survive a reboot — tell the owner); then key rotation — done by the owner.
- `logins gap` («журнал переполнен»): a brute force is under way; look at `fail2ban-client status sshd`, propose enabling fail2ban/restricting SSH by IP (done by the owner).

## New port or another process on a port (`ports`)

Look: `ss -tlnpu`, `ps -p <pid> -o user,etime,cmd`, `ls -l /proc/<pid>/exe`, `ps -p <pid> -o args` (the arguments: the gate refuses `/proc/<pid>/cmdline`), `stat -L /proc/<pid>/exe` (the binary; the gate does not allow `stat` on a binary path outside `/etc`, `/opt`, `/var/www`, `/var/log`), `systemctl status <unit>`.
Legitimate (deploy, new service) — ask the owner; after his «да» — `accept`. Not legitimate — evidence first, by card: `mkdir -p /tmp/kd-evidence-<дата> && cp /proc/<pid>/exe /tmp/kd-evidence-<дата>/<pid>.bin` (malware has often already deleted its file, and after a kill `/proc/<pid>/exe` is gone too); only then, in a separate card, `kill <pid>` (not on example-stage) and isolation of the IP. Write the date as a literal (`2026-10-06`): the hook rejects command substitution.

## Keys, sudoers, users, groups, sshd, shadow (`sec:keys`, `sec:sudoers`, `sec:users`, `sec:groups`, `sec:sshd`, `sec:shadow`)

Look: `cat /etc/passwd`, `cat /etc/group`, `cat /etc/ssh/sshd_config`, `ls -l /etc/ssh/sshd_config.d`, `stat` of the changed file (who and when), auth.log around the time of the change. The gate does not return the content of `authorized_keys`, `sudoers`, `shadow` — only hashes, line counts and `symlink -> target` in the snapshot (a symlink in place of authorized_keys is always suspicious; for authorized_keys there is only the snapshot: the gate does not allow even stat). For each key of `authorized_keys` and of `~/.ssh/*.pub` the snapshot also has its type, SHA256 fingerprint, option names (`command` — a forced command) and comment, never the key itself: the `sec:keys` event names the keys added and removed (or says the baseline has no fingerprints of that file yet), and `kd-monitor keys <отпечаток|сервер|комментарий>` shows where else that key may log in, on which server its `.pub` lies (whose key it is) and where a login with it is accepted. Answer «чей ключ» with that, not with a request to read the file.
Roll nothing back yourself. Tell the owner: what changed, when, who was on the system; ask whether it was him. His — `accept`; not his — the owner fixes it by hand, you propose isolating the IP. The `staff` group on Ubuntu writes to `/usr/local/bin` — a new member of that group matters too.

## Cron, persist and units (`sec:cron`, `sec:persist`, `sec:units`)

Look: `cat` of the file (for `/var/spool/cron` — only `crontab -l -u root`: the gate does not read the directory and does not return another user's crontab — the owner will show its content), `systemctl cat <юнит>`, `stat`. The gate refuses `cat`/`stat` outside its roots: a `sec:persist` or `sec:cron` object whose snapshot value is `symlink -> /root/…` (or another path outside `/etc`) can be seen only as that snapshot value (hash, line count, `symlink -> target`) — ask the owner to show the content. `/etc/ld.so.preload` appeared — almost certainly a rootkit: evidence (`cat`, `stat`, or the snapshot value if the gate refuses), straight to the owner, touch nothing. A foreign unit — propose by card `systemctl disable --now <юнит>` and `rm /etc/systemd/system/<юнит>` (beforehand `mkdir -p /tmp/kd-evidence-<дата> && cp /etc/systemd/system/<юнит> /tmp/kd-evidence-<дата>/`, date as a literal). A foreign cron file: the agent may not delete files in `/etc/cron.d` and `/var/spool/cron` (denylist `cron-wipe`) — show the owner the content and the path, he deletes it. Looks like his own (deploy, backup, snap) — ask the owner; after his «да» — `accept`.

## Firewall and fail2ban (`sec:fw`, `posture`)

`ufw status verbose`, `fail2ban-client status sshd`, `iptables -S`. The agent may not enable ufw (denylist: on a server without rules, enabling ufw cuts everyone off) — the owner does that; fail2ban is down — propose `systemctl restart fail2ban` by card. `ufw:default` became `allow (incoming)` — 🔴, straight to the owner.

## WireGuard peer (`wgpeers`)

`wg show all endpoints`, `wg show all latest-handshakes`. Looks like a new channel of the owner — ask the owner; after his «да» — `accept`; unknown — report; `wg set <iface> peer <ключ> remove` by card only after «да», and remember: it does not survive a wg-quick restart, the owner edits the config.

## Load (`load`)

`top -bn1`, `ps -eo pid,user,%cpu,etime,cmd --sort=-%cpu`. An unknown process with high CPU — as with a «new process» above (a miner: binary in `/tmp`, `/dev/shm`, strange names; look at `ls -l /proc/<pid>/exe`, `stat -L /proc/<pid>/exe`, `ps -p <pid> -o args` — the gate does not list `/tmp` and `/dev/shm` themselves). A known one (mysqld, consumers, the nightly dump on prod) — an ordinary performance review; if it recurs regularly — ask the owner; after his «да» — `ack`.

## Updates and reboot (`updates`, `reboot`, `autoupdates`, `eol`)

- VPN nodes: propose by card `apt-get update && DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold install <security-пакеты>` (without these flags a dpkg config question leaves packages half-configured, and needrestart restarts services) (AlmaLinux: `dnf -y update --security`), the reboot — in a separate card.
- example-prod and example-stage: only under the OS update procedure (CLAUDE.md), the window is agreed by the owner; example-stage is rebooted only by the owner by hand.
- Auto-updates are off — propose enabling `unattended-upgrades` (only `-security`; on prod remember the mysqld restart — `Unattended-Upgrade::Package-Blacklist` or a window; a blacklisted package will keep hanging in `updates` — the owner silences it with `ack`) / `dnf-automatic-install.timer`.
- End of OS support — a note to the owner with the date, the upgrade plan is discussed separately. Hourly and daily checks close on the first good run after the fix.
- In the weekly line «безопасность: security-обновлений N (старейшее K дн)» K is whole days since first detection, rounded down: an update found today is «0 дн».

## A check is not working (`health`)

The text says exactly what: no `agent-sec-snapshot` (not deployed — developer), no `auth.log` (the monitor switches to journal by itself), `dnf` without a cache (`systemctl is-enabled dnf-makecache.timer`). Without `needrestart` the monitor reads `/run/reboot-required` itself (do not propose installing needrestart: it restarts services on its own); `reboot` still not working there means the gate on that server is older than KD-185 — tell the developer. Propose to the owner installing what is missing by card, or tell the developer if it is the cage.
Memory thresholds live in `agents/sysadmin/monitor/config.json`: percent for all servers, megabytes for small ones (`mem_available_mb.<сервер>`, node-a — 100 MB); a change is a developer card.

## Audit (on the owner's word «проведи аудит»)

For each server via `ssh-ro` (`agent-sec-snapshot`, `ss -tlnpu`, `cat /etc/passwd`, `cat /etc/group`, `cat /etc/ssh/sshd_config`, `ls -la /etc/sudoers.d` (exactly so: the directory is root 0750), `ls -l /etc/cron.d`, `ufw status verbose`, `fail2ban-client status sshd`, `systemctl is-enabled apt-daily-upgrade.timer`):
1. sshd: `permitrootlogin` → `prohibit-password` where login is by keys (example-prod, node-b — currently `yes`); `passwordauthentication no`; `kbdinteractiveauthentication no`; `Match` blocks in `sshd_config`.
2. Leftover accounts: Alice (on node-a `DenyUsers Alice` — do the user, his keys and groups still exist?), other users with a shell, extra keys in `authorized_keys` (`kd-monitor keys <сервер>` — each key with its comment and where else it lies), `/etc/sudoers.d`, members of `sudo`/`docker`/`staff`.
3. fail2ban and ufw (or raw iptables with an explicit policy) on every server; SSH by IP where addresses are permanent.
4. Security auto-updates on all Ubuntu servers, `dnf-automatic-install.timer` on node-b.
5. Listening ports: for each — is it needed from outside; close the extra ones or bind them to localhost.
6. Cron, persist and units from the snapshot list: anything unfamiliar — ask the owner.
7. Secrets and permissions (stage 1 backlog): proxy provider credentials in logs, `?token=` in nginx logs, a password on a proxy unit's command line, `config.ini` 664 on staging, `hidepid`.
8. Backups on node-b: readable only by root.

The result — one report to the owner per server (ok / proposal), each fix — a separate card, example-prod last. After the fixes: first propose `kd-monitor trust` for the owner's addresses, then `kd-monitor baseline all`; once he signs, the monitor takes the baseline and the decision's result says which servers it covers — tell him the ones it missed. After any run of the installers (`deploy_target.sh`, `new_agent.sh --install sysadmin`) the owner retakes the server's baseline — they change sudoers and units. If a snapshot section had an error when the baseline was taken, retake the server's baseline after the fix.
