# Deployment overlay

The factory's code repository holds no values of a concrete deployment: no host names, addresses,
domains, inventory or forge accounts. A deployment keeps them in a separate private repository —
the **overlay** — whose files are laid over the code tree when a release is built. This document
is the contract between the two.

## What a release is

`kd-release` builds one tar and switches the release symlink to it:

1. `git archive` of the code repository's `main`;
2. the overlay repository's `main`, **only** the paths listed below (a code file with the same path
   is replaced; every other overlay path — internal docs, the overlay's own `CLAUDE.md`, tests —
   never reaches a host);
3. a `DEPLOY_REVISION` file with the overlay commit (7 hex), next to the code's `REVISION`.

The release identity is `release_id = <REVISION>+<DEPLOY_REVISION>`. Operations a release orders
(agent reinstalls, component steps) are keyed by it, so a release that changes only the overlay is a
new release.

An overlay is **data only**: nothing of it is ever run. Before the switch, in this order:

1. the installed release tool (the running tree's `kd.overlay`, not the code being released) checks
   the overlay archive as data — safe paths, regular files, and every file of an allowed path a data
   file (below) with no executable bit and no `#!` line; `host/overlay.json` has an integer
   `schema_version` (its range is the released code's);
2. the privacy scanners run over the **code tree alone** (the overlay carries addresses on purpose):
   the released code's public scanner with its `privacy-scan.json`, and the installed tree's closed
   scanner `common/kd/private_scan.py` with the overlay's literal list `host/private_scan.json`.
   Both read every file of the tree: a file with a NUL byte in its first 8 KiB is binary and is read as
   bytes (UTF-8 and UTF-16 of either byte order; a closed literal must be whole), never skipped; a
   finding there carries a byte offset in place of a line. `kd-release` and `kd-mirror` both refuse a tree
   with a symlink or a submodule (`common/kd/plain_tree.py`), and `kd-mirror` publishes nothing of such a
   main;
3. the released code's `kd.overlay` validates the overlay on its own (schema range, valid JSON, no
   secret-like strings), composes the tree and checks it for every required file and a valid
   `host/host.json`.

Steps 2 and 3 run code that is not yet released, so they run in a sandbox: a transient
`systemd-run` unit with a dynamic unprivileged user, no network, no home directories, no
`/etc/digipaddock` or `/var/lib/digipaddock`, the work directory bound read-only, and only the
unit's own `/tmp` writable (the composed tar comes back on stdout). Any failure refuses the release
(`release: privacy`, `release: overlay_invalid`, `release: overlay_incompatible`) and nothing is
switched.

Code reads overlay values from the release tree (`/opt/digipaddock/host/host.json` and the
paths below), never from `/etc`: rolling a release back rolls the overlay back too. `/etc` keeps
only secrets and host state.

## Paths an overlay may provide

| Path | What |
|---|---|
| `host/` (any file) | `host.json`, `overlay.json`, the private scanner's list and other host values |
| `agents/sysadmin/inventory.json` | servers of the deployment, with `home` — the alias of the factory's own host; a server's `data_databases` are the databases `kd-data` reads there, `data_hidden_columns` (`<db>.<table>.<column>`) the secret columns beyond the name rule |
| `agents/sysadmin/monitor/config.json` | monitor settings per server |
| `agents/sysadmin/operations.json` | the sysadmin's catalogue of typed operations; `verbs` lists `script` to allow plan steps of exact text — the legacy `shell` is dropped when the catalog loads. `apps` names the project environments whose own steps a plan may carry (the verbs `app.tool`, `app.flag`, `service.reload`): per environment its `server`, the application's `user` (never root or an account of the platform) and `dir` (under `/var/www`, `/opt` or `/srv`, outside the platform's trees), its `python` (`python3` or a path inside `dir`) with the `tools` it may run (`.py` scripts inside `dir`) and/or, for a PHP application, `php` (the system's, by its bare name) with the `spark` commands a step may run, and, optionally, `flags` — for an env file `{"file": ".env", "format": "env", "keys": {"<KEY>": ["<value>", …]}}`, the keys a step may set and the plain-word values each may take; for an ini file `flags` — the config `file` and the `sections` whose `…_enabled` switches may be set; the example file shows one. `"test": true` marks the environment as a test one — a plan made only of such environments' steps runs without the owner's signature (never on `prod_alias`: the catalog does not load) — `reload` lists the units of its server such a plan may reload, and `read` lists directories of its tree (relative, such as `logs/api_journal`) the sysadmin reads whole through the read-only gate, whatever the application's own file modes (the gate's forbidden paths still apply; `zcat`/`zgrep` read the `.gz` archives there too); `deploy_target.sh <alias>` carries the server's test environments to it, so a changed mark takes effect there only after that run |
| `agents/sysadmin/playbooks/security.md` | security playbook naming real hosts |
| `agents/sysadmin/servers.md` | server table included into the sysadmin's instructions |
| `common/install/forgejo_known_hosts` | SSH host keys of the forge |

Every path except `host/` is required in a composed release. A file of these paths is data: a
`.json` or `.md` file, or one of `host/allowed_signers` and `common/install/forgejo_known_hosts`;
text, without an executable bit or a `#!` line. Anything else there refuses the release
(`overlay_invalid`). Other paths of the overlay repository (its docs, its own tools) never reach a
host and nothing runs them.

The code repository ships `*.example.*` stand-ins for the `host/` files and the sysadmin's files
(inventory, servers, security playbook); the closed scanner's list starts from
`host/private_scan.example.json`. The closed scanner itself is code (`common/kd/private_scan.py`);
an older overlay's `scripts/private_scan.py` is no longer an overlay path, and a release tool that
has this rule never runs it. The forge's known hosts are the deployment's own.

## Who changes what

| What | Where | Who changes it |
|---|---|---|
| The release tool: `kd-release`, `kd.overlay`'s data check, both scanners, the sandbox | the code repository | a pull request into `main` the owner approved on its head; it acts from the release after the one that brings it |
| The deployment's parameters: host values, inventory, the scanner's literal list | the overlay repository | a pull request into its `main` the owner approved on its head; data only, checked by the installed tool |

`main` of both repositories is protected on the forge the same way: the owner's account
(`forge_owner`) is the only approver and the only direct pusher, the merge account merges only a
pull request he approved on its head, and the agents' accounts push only proposals
(`refs/for/main`). A release takes nothing else — no branch, no other repository, no file of the
overlay outside the paths above. Since nothing of the overlay runs, write access to the overlay's
`main` changes a deployment's values, never the code a release runs as root.

## The deployment's own rules (optional files in `host/`)

What only one deployment has — its applications' paths, hosts and tools — stays out of the public code; optional
files next to `host.json` add it. Without a file only the public rules apply; a file that does not parse fails closed.

| File | Read by | What it adds |
|---|---|---|
| `host/sysadmin_rules.json` | the sysadmin's denylist (`ssh-exec`, `kd-admin-propose`) | `code_trees` — application trees besides `/var/www` that change only through git and deploy; `socks_only_hosts` — host-name labels the servers reach only through a SOCKS proxy; `dry_run_only` — `{"program", "subcommand"}` pairs that run only with `--dry-run`. A broken file refuses every command (rule `deployment-rules`) |
| `host/protected.json` | the capability registry (every agent's policy) | `protected` entries in the shape of `common/agent-resources.json`: secret paths on the agents' host; it only adds denials. A broken file fails the registry |
| `host/egress.json` | the cabinet (which card session gets a probe exit), `kd.launch`, `egress_setup.sh` | Exits: each exit's host names (or `["*"]`, every host), its loopback port and VPN SOCKS port, the agents that may use it, which project maps to which exit, the optional `standing` exit every session of its agents holds, and the optional `countries` table (ISO 3166-1 code → exit of every host) a card's `egress:<code>` label chooses from ([probe-egress.md](probe-egress.md)). The release refuses a broken file or an exit listing an agent without `net.egress`; no file means no exits |
| `host/gate_forbidden.json` | `deploy_target.sh` → `target_setup.sh --forbidden` → the read gate on each server | `forbidden` — regular expressions of the deployment's secret paths, installed as `/etc/agent-ro-gate/forbidden.json` (root, 0644). A file there that is not root's, is writable by others or does not parse refuses every path |

The code repository ships `host/*.example.json` stand-ins for them; the sysadmin's `servers.md` explains the same
rules to the agent in words.

## `host/overlay.json`

```json
{"schema_version": 1}
```

The code supports overlay schemas `kd.overlay.OVERLAY_SCHEMA_MIN..OVERLAY_SCHEMA_MAX`.

## `host/host.json`

Schema 1; every key is a non-empty string. Repository files refer to a key as `@host:<key>@`
(units, nginx templates, managed settings, capability registries — rendered by `kd.naming.render`)
or `{{host.<key>}}` (agent instructions and playbooks). Shell installers read one value with
`python3 -I common/kd/naming.py host <key>`. A missing key or a wrong type fails closed.

| Key | Meaning | Example |
|---|---|---|
| `stage_alias` | SSH alias of the factory's host | `example-stage` |
| `stage_ip` | its public address (installers check they run on it) | `203.0.113.10` |
| `prod_alias` | alias of the production server | `example-prod` |
| `forge_host` | host name of the forge (Forgejo) | `forge.example.test` |
| `forge_ssh_url` | SSH clone URL, `{repo}` = `owner/name` | `ssh://git@forge.example.test/{repo}.git` |
| `forge_api` | forge API base | `https://forge.example.test/api/v1` |
| `cabinet_host` | host name of the cabinet (web board) | `cabinet.example.test` |
| `cabinet_origin` | its origin | `https://cabinet.example.test` |
| `docs_dir` | directory of the product documentation agents read | `/srv/example-docs` |
| `mirror_remote` | push URL of the public mirror | `git@github.example.test:example/digipaddock.git` |
| `dev_email` | commit e-mail of the developer agent's forge account | `dev@example.test` |
| `forge_owner` | the owner's forge account: the only approver and pusher of protected `main` | `example-owner` |
| `forge_dev_user`, `forge_merge_user`, `forge_review_user`, `forge_import_user` | forge accounts of the developer agent, of merges, of approvals from the cabinet, of pull requests imported from GitHub; their token files are named after them | `example-dev` |
| `owner_language` | the language the owner reads, `en` or `ru`: the agents write to the owner in it, and it is the cabinet's default for a person who chose none and the language of notices and alerts; required — a host.json without it fails the overlay check, so a deployment never changes language by an update | `en` |
| `timezone` | the host's zone (an IANA name): days of the spending ledger, the monitor's clock; optional, `UTC` by default | `Europe/Berlin` |
| `mirror_author` | author and committer of the snapshot commits on the public mirror, `Name <email>`; optional, `DigiPaddock <mirror@localhost>` by default | `DigiPaddock <mirror@example.test>` |
| `backup_dir` | where the host's own dumps go (`<backup_dir>/hindsight`), the directory a backup mirror takes; optional, `/var/backups/digipaddock` by default | `/srv/backups/db` |

See `host/host.example.json`.

## `host/install.json` and `host/projects.json`

What `bootstrap.sh` installs ([install.md](install.md)); optional for an overlay that only releases.

- `install.json` — `naming` (`prefix` of the Linux users, `commands`, `cards`; written to the host once, at the
  first install), `roles` (agents of the code tree with their session slots), `components`, `pipeline`
  (`observe` or `enforce`), `hindsight`, `behind_nat` (the cabinet's DNS name, not an interface, carries the host's
  address). The users' prefix may not be a command prefix, and the command prefix may not be one of the host's
  own tools (`apt`, `git`, …).
- `projects.json` — the board's projects: `name`, `route` (`@owner` or a role other than the coordinator),
  `done_by` (`""` — the owner accepts — or a role), `memory_bank`, and optionally `repo`: the project's entry of
  `/etc/digipaddock/repos.json` exactly as `add_repo.sh` writes it. The project whose `repo.release` names the
  release and whose `role` is `code` (the default) is the code repository of the release; the overlay's own is
  `role: overlay`. `repo.deploys` names the environments the cabinet's «Deploys» shows, in order:
  `{"test": {"workflow": "deploy-test.yml", "branch": "master"}, "prod": {"workflow": "deploy-prod.yml",
  "branch": "master", "manual": true}}` — the Forgejo Actions workflow file that deploys each (one file per
  environment), the branch it deploys and whether it runs only by hand. A manual one gets the owner's button
  «Deploy to prod» ([cabinet.md](cabinet.md)): `"confirm": "<input>"` names the workflow's text input that the approver fills
  with the word `deploy` (the owner types nothing), `"skip_tests": true` says the workflow takes the boolean input `skip_tests`; both only with
  `"manual": true`. The deploys reader reads it from the
  release tree at every run, so a change is live after the release, with no reinstall.

See `host/install.example.json` and `host/projects.example.json`.

## `host/allowed_signers`

Optional. SSH public keys, in `ssh-keygen -Y` allowed-signers format, of who signs release tags. With it,
`kd-cabinet release <name> --rev v…` deploys a tag only when its signature verifies; without it a tag release warns.

## Checking an overlay

```sh
python3 -I common/kd/overlay.py init <dir>    # a new overlay from the examples
python3 -I common/kd/overlay.py check <dir>   # what a release would refuse, before pushing it
```

`check` also names every file a release needs that neither the overlay nor the code tree carries: the forge's known
hosts, the closed scanner's list (`host/private_scan.json`), and a list still holding the example's literals
(the code tree's own example names them, so every release would be refused); a file of an allowed path that is not
data (an executable, a script) is an error. `init` writes every file that has an example (the `host/` files, the
sysadmin's) under its overlay name and lists the rest. `bootstrap.sh check` runs the same `check` on the release tree.

## Compatibility

Code and overlay are released together but change separately:

- a new key — code first, reading it with a default; overlay second. A key becomes required
  (`HOST_KEYS`) only once every overlay has it — otherwise the release refuses
  `overlay_incompatible`;
- a removed key — code stops reading it first, the overlay drops it second;
- extra overlay keys are not an error.

The move of the closed scanner into the code goes in this order: first the release of
the code that carries `common/kd/private_scan.py` — the release tool still running then is the old
one, which runs the overlay's `scripts/private_scan.py` one last time and refuses an overlay
without it; then, from the new release tool on, a pull request into the overlay removes
`scripts/private_scan.py`. Removing it first stops every release until it is back.
