# Hindsight in the cabinet

`/settings/hindsight`, linked from Settings, is available with the global `settings` permission.
It reports observed state; it never upgrades packages, changes memory routing, triggers synthesis or creates banks.
On a host without the memory service (no `/etc/hindsight/coding-agent.env`, `memory` of `/api/me` false) Settings
has no link to it, the page says Hindsight is not installed, and projects have no memory bank field; see
[install.md](install.md#with-or-without-the-memory-service).

The cabinet remains restricted to Unix sockets. `digipaddock-hindsight-status.timer` runs a separate collector every five
minutes. Its root process reads the installed clients of the registered agent users, checks the loopback API,
and atomically publishes `/var/lib/digipaddock/hindsight-status/status.json`. Only bank identifiers, counts,
timestamps, allowlisted policy fields, versions and unit states are published. No API keys, connection strings,
memory contents, prompts or upstream error bodies are copied. Files are bounded, regular files without file
symlinks. The cabinet serves the snapshot through `GET /api/hindsight` after checking permission.

`common/install/cabinet_setup.sh install` installs and enables the service and timer along with the cabinet.
The collector runs only if `/etc/hindsight/coding-agent.env` exists. The first snapshot arrives after the timer
runs; missing or older-than-15-minute snapshots are shown as unknown. The page rereads once a minute while
visible. “Refresh data” rereads the snapshot, without invoking a privileged command.

Server and client package versions are checked against public PyPI and npm metadata at most once daily.
Failed comparisons are unknown. The page shows the actual installed plugin version and `autoUpdate` policy
per agent; the server remains an administrator-managed installation. Checking for updates installs nothing.

The configured Hindsight API is `http://127.0.0.1:9077`; it is not the web interface. To offer an existing,
protected control plane, set `hindsight_web_url` in `/etc/digipaddock/cabinet.json` to its HTTPS URL.
Credentials and query parameters are not accepted in this URL. An absent URL is reported as unconfigured,
not proof that a control plane is disabled. This change does not deploy or expose a control plane.

Bank operations for the rolling seven-day period use Hindsight's audit statistics when the `audit_log`
feature is enabled. The server's env enables it (`HINDSIGHT_API_AUDIT_LOG_ENABLED=true`, kept 90 days) and keeps
the server's own model calls with their tokens per operation for eight days
(`HINDSIGHT_API_LLM_TRACE_RETENTION_DAYS=8`, table `llm_requests`). Disabled, unsupported or failed audit
statistics remain unknown.

A knowledge page refreshes under the server's reflect wall timeout, raised to 900 s
(`HINDSIGHT_API_REFLECT_WALL_TIMEOUT`): with two LLM slots shared with consolidation, a refresh at the default 300 s
was mostly cancelled, and Hindsight pauses the scheduled refresh of a page whose last refresh failed until an explicit
refresh succeeds (the worker's log: `… up-to-date, N paused`). A bank shared by several repositories should reach
them all the same way (all through `mapPathToBank`, or none): the plugin scopes the five base pages to the
repository's name when it resolves the bank by the folder and to the bank's id when a mapping does, so two ways
rewrite the pages back and forth and each rewrite rebuilds all five. Internal model spend
comes separately from `kd-usage`'s ledger over seven calendar days in the ledger's UTC+03:00 timezone,
including today. The ledger is updated daily; absent and partial days keep their coverage labels. This is
not a count of agent memory accesses, and subscription usage is not an additional API invoice.

Bank counts, pending work, failed work and last write use the upstream
[Hindsight API](https://hindsight.vectorize.io/api-reference). The collector only issues GET requests, so
diagnostics do not incur LLM synthesis. An active backup timer is shown as such, without claiming that a
backup completed or that recovery was tested.

## Project memory

A cabinet project may name a Hindsight bank: Settings → Projects → «Hindsight memory bank» (`memory_bank` of
the project record, set with `settings`; partners never see it). The binding is by project ID, never by
repository or folder name, so renaming or splitting repositories changes nothing in memory, and several
projects of one product may share one bank.

- **One selection per session.** When the dispatcher delivers a card's command, it adds the bank of the
  card's project (`memory` of the `deliver` request). The session host starts the card's session with
  `agent-launch --memory <bank>`; the launcher, as the agent's user, copies the agent's role config
  (`~/.hindsight/coding-agent.json`) to `~/.hindsight/bank-<bank>.json` with `bankId` set and without
  `mapPathToBank` (the plugin prefers a mapped folder to `bankId`), and points `HINDSIGHT_CONFIG` at it. The
  plugin reads its config per process, so every hook and the memory tools of that session read and write that
  one bank; the session note names it. A new binding holds for the sessions started after it — a running
  process keeps its bank until it exits.
- **Role bank stays** for the agent's chat and “Monitor” sessions, for cards of projects without a bank,
  and whenever the bank config cannot be written (the launch goes on with a warning). Another project's bank
  is never substituted. Workers and check runs stay without memory; read-only project memory for workers is a
  separate decision.
- **This page** shows each bank with its agents (role banks, from the clients' `mapPathToBank`) and its
  projects, and each project with its bank or «Role memory». A bound bank the server does not have yet is
  listed as missing; nothing is created to diagnose it.
- **Access is configuration, not enforcement.** Self-hosted Hindsight has one tenant key and no per-bank
  ACL, so bank isolation between roles and projects is a client-side convention; this page stays behind the
  `settings` permission. Per-bank keys would need a tenant extension (a separate option).
- **Migration is a binding, not a rename or merge.** The existing developer bank becomes the bank of the
  factory's own projects; a new project's bank starts empty unless the owner imports into it; banks of the
  owner's own sessions in the same tenant are left untouched.
- **Host restart.** The cabinet sends `memory` only to a host that knows it: an older host refuses the
  field and gets the same delivery without it (role bank) until `digipaddock-session-host.service` restarts.
