# Cabinet command line: `kd-cabinet`

`kd-cabinet` is the cabinet's own program on the factory host. One process of it, `kd-cabinet serve`, is the
service that owns the board database; every other subcommand is a client that talks to that service over its local
socket and is checked there by who runs it (`SO_PEERCRED`). The link `/usr/local/bin/kd-cabinet` points into the
current release tree, so a release switch changes the program and the service together.

Who may run what:

- **root** — everything below unless the line says otherwise. Writes made as root are signed `root` on the card
  (its thread and history show them that way). A person's clicks stay in the web; root's writes are the
  maintainer's own cards, clean-ups and the host's operations.
- **anyone in the cabinet's socket group** (the base user, the executors, the agents' users) — the read-only
  `cards` and `card`.
- **systemd** — `serve` (the service), `export` (the nightly timer), `push` (the push sender).

Exit codes: `0` done, `1` the board is unavailable or the command failed, `2` refused or invalid input (usage
errors included), `3` conflict (the card changed under the command).

The web's own operations (a person's decisions, the agents' writes through `kd-task` and `kd-work`) are not here;
see [cabinet.md](cabinet.md) for the screens and [roles.md](roles.md) for the agents' commands.

## Board: reading

| Command | What it does |
|---|---|
| `kd-cabinet cards [-s STATUS] [-a @who] [-p PROJECT] [-l LABEL]` | One line per card: `KD-n · status · assignee · title [labels]`. Filters combine. |
| `kd-cabinet card KD-n` | The card in full: header, description, acceptance criteria, the thread's messages. |

## Board: writing (root)

| Command | What it does |
|---|---|
| `kd-cabinet create "<title>" -p PROJECT [-a @who] [-d TEXT]` | A new card in Inbox for the project; the assignee defaults to the project's route (`kd-task projects`). Prints the card's line. |
| `kd-cabinet edit KD-n [-s STATUS] [-a @who] [--priority P] [--check-ac N]… [--uncheck-ac N]… [-c "comment"]` | Field changes and an optional comment at the revision just read; at least one change or a comment is required. `-s Cancelled` closes the card without the work (outcome `cancelled`, a reason in the comment); a card closed as Done by mistake may be moved to Cancelled the same way. |
| `kd-cabinet edit KD-n --duplicate-of KD-m [-c "comment"]` | Closes KD-n as Cancelled, a duplicate of KD-m (KD-m must exist and differ; no other status goes with it). |
| `kd-cabinet defer KD-n --until MOMENT [--reason TEXT]` | Puts the card off until a moment — the same as the owner's “Put off until…”: out of the inbox and the agents' attention until then, woken by the cabinet with a notice. The moment carries its time zone (`2026-11-16T09:00+03:00` or `…Z`), at least 10 minutes away, at most 90 days. A closed card is refused. |
| `kd-cabinet defer KD-n --wake` | Ends the card's defer now (the held wakes go at once). Refused when the card is not put off. |

## People and keys (root)

| Command | What it does |
|---|---|
| `kd-cabinet user add <id> --name "<name>" --role owner\|partner\|viewer [--projects a,b]` | Creates a person and prints their one-time sign-in code (15 minutes). `--projects` scopes a partner or viewer. |
| `kd-cabinet user code <id>` | A new sign-in code for an existing person (the previous unused one stops working). The owner can also issue codes in the web. |
| `kd-cabinet user block <id>` | Blocks the person and ends their sessions. |
| `kd-cabinet user list` | The people with their roles and status. |
| `kd-cabinet user signing <id> --credential PREFIX on\|off` | Whether one passkey of the person signs: server plans, critical commands, PRs, factory steps — every passkey step-up in the cabinet uses the same flag. `PREFIX` is the start of the key's credential id, 8 characters at least. A new passkey signs by default. |
| `kd-cabinet user signers [ID…]` | The registry of signing passkeys as JSON (`rp_id`, `origin`, one entry per key with its public key): what `deploy_target.sh` installs on a managed server for its exec gate. Without ids — every active owner; a named person without a signing key is an error. |

Two services keep their own copies of the owner's keys: kd-review (PR approval) and kd-model-keys (provider keys).
Managed servers keep the set they were first installed with. After a passkey is added, removed or its signing switched,
root brings the copies to the cabinet's set with one command:

| Command | What it does |
|---|---|
| `kd-signers status` | For every active owner compares the cabinet's signing passkeys with kd-review, kd-model-keys and the exec gate's registry of every server of `servers.enabled` (read through the read-only gate, with its `rp_id` and `origin`). One line per registry: `ok`, the `extra` and `missing` keys (name and the first 8 characters of the id), or `unread: <reason>` — an unread server stops none of the others. Exit code 0 — every registry matches, 1 — one differs, 2 — one was not read, 3 — refused. |
| `kd-signers sync` | The same status and the plan for kd-review and kd-model-keys; nothing is written. A server whose set differs gets the line the owner runs on his own machine, `deploy_target.sh <alias> --replace-signers`, the production server last. |
| `kd-signers sync --apply` | Writes the plan: both registries take the cabinet's set (a new key joins, a removed or switched-off one leaves), each file atomically with its owner and mode kept; other people's registries are not touched. Then prints the status again. An owner with no signing key, or a registry file it cannot read, refuses the whole sync before anything is written. |

The cabinet's set is the truth; the servers are never written from this host — it is the one their exec gate guards
against, so only `deploy_target.sh <alias> --replace-signers` from the owner's machine changes a server's set. The read
gate masks a server's public keys, so a server is compared by owner and credential id (and its cabinet). One key at a
time still works: `kd-review signers add <user> --credential <id>` and `kd-model-keys enroll <owner>
[credential-prefix]`.

## Releases and health (root)

| Command | What it does |
|---|---|
| `kd-cabinet release <project>` | Queues a release of the project's `main`. The command prints the path to this request's terminal result. |
| `kd-cabinet release <project> --rev SHA\|TAG` | The same for a named commit on `main` or a tag `v…` (its signature is verified). |
| `kd-cabinet release <project> --rollback [DIR]` | Switches back to the previous release, or to the named release directory. |
| `kd-cabinet health [--json]` | Runs the release health checks on the current tree without a card or a notice: exit `0` healthy, `1` degraded. |

For the outcome of a queued release, wait for the printed `result-<request_id>.json` path. Its `state` is
`refused` if the tree stayed in place, `degraded` if an obligatory step failed after the switch, or `healthy`
after the checks passed. A degraded result lists the installed pair and failed steps. Optional failures are in
`warnings`. The service's `active.json` shows the currently running request and its phase. Use `result.json` for a
summary of the latest completed release. It can change whenever another release finishes.

`kd-merge --release` reuses the same request for that repository, PR and merge commit. A repeated call can retrieve
the saved outcome after a timeout. To intentionally start another release of the same commit, use the root command.
If the release service is interrupted and restarted, it records failure for the interrupted request and checks
whether the tree switched before returning the result. An older worker still running during an upgrade may supply a
terminal result through the legacy latest-result file. Wait for that worker to finish before submitting new requests
if the per-request history is required from the first new request.

## Operations, checks and contours (root)

| Command | What it does |
|---|---|
| `kd-cabinet operation request --agents a[,b] [--card KD-n] [--key K]` | Asks the session host for the factory operation `agent.reinstall` of the named roles — no grant, root owns the host. Prints `operation <id> <state>`; the key defaults to `root:<agents>:<time>`. |
| `kd-cabinet operation get <id>` | The operation's state and result. |
| `kd-cabinet devcheck list [--card KD-n]` | The dev's checks (`kd-forge check`), newest first, one line each. |
| `kd-cabinet devcheck cancel <id>` | Stops a check: ended here if it has not reached the host yet, otherwise flagged for the host to stop. |
| `kd-cabinet contour list` | The check contours with their state and what runs in each. |
| `kd-cabinet contour maintenance <name> --reason "…"` | Takes a contour out of service with the reason; new checks wait. |
| `kd-cabinet contour release <name>` | Returns the contour to service. |
| `kd-cabinet pipeline status` | The pipeline's counters: outcomes waiting for their turn's end, open waits, continuations, problems. |
| `kd-cabinet pipeline inventory [--json]` | Read-only inventory of stuck cards by class (seven classes), for a review of the board. |

## Service commands (systemd)

| Command | What it does |
|---|---|
| `kd-cabinet serve` | The service: the only process that opens the board database. It takes the local socket (and the HTTP socket for the web) from systemd; without `cabinet.json`/`cabinet.env` it serves the local socket alone and the web stays off. |
| `kd-cabinet serve --dev [--seed] [--listen HOST:PORT] [--origin URL] [--state-dir DIR] [--db FILE] [--language ru\|en]` | The whole cabinet on a developer's machine over a state directory (default `tmp/cabinet-dev`), seeded once, with a scripted session host (one turn per command; the critics of a council read and find nothing) and a synthetic repository for the seed's project (a council of the code needs one); the dev-only routes are on. |
| `kd-cabinet export <board_dir>` | Writes the board as Markdown files and commits them to the directory's git (the nightly snapshot); prints `committed` or `clean`. |
| `kd-cabinet push` | The Web Push sender until it is stopped; exit `1` when the host has no VAPID key. |

## Where to look when a command refuses

- `refused` / `invalid` (exit `2`) — the message names the field or the rule: the status needs an outcome, the
  moment has no time zone, the card is closed, the person is not an active owner.
- `conflict` (exit `3`) — the card changed between the read and the write; run the command again.
- exit `1` with «board unavailable» — the service or its socket is down (`systemctl status` of the cabinet service);
  with another message — the command itself failed and says why.
