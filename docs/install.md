# Installing DigiPaddock

This guide takes one host from an empty Ubuntu 24.04 to a running factory: the cabinet, the session host, the
agents your deployment chose, and their memory. One script, `common/install/bootstrap.sh`, runs the installers in
order. Every step checks first and installs only what the check finds missing, so you can run it again at any time.
When a step needs you (a key in the forge, a token, a Claude login), the script stops and says what to do.

Installation configures Linux accounts, permissions, systemd services, network access and approval boundaries.
Run it only on a host meant for the factory, with the owner's decision to do so.

## What you need

### The host

| | Without the memory service | With the memory service | With every component |
|---|---|---|---|
| System | Ubuntu 24.04, x86-64 or arm64, systemd, root access | same | same |
| Machine | Virtual or physical, not a container: the agents' sandbox loads an AppArmor profile into the kernel, which a container shares with its host | same | same |
| Memory | 2 GB | 8 GB: its API and worker hold about 2 GB each with their models | 8 GB |
| CPU | 2 vCPU | 4 vCPU: its embedding and reranking models run on the CPU, four threads each | 4 vCPU: transcription runs a Whisper model on the CPU |
| Disk | 20 GB | 30 GB | 40 GB |
| Address | A public address, or an address behind NAT that the cabinet's DNS name points to | same | same |

Every running agent session adds a few hundred MB of memory. "The memory service" is Hindsight, the agents'
long-term memory (see [With or without the memory service](#with-or-without-the-memory-service)); the components
are those of `install.json`.

### Disk by part

Measured on a running deployment:

| Part | Disk |
|---|---|
| Code, release trees, the cabinet's environment and web | about 1 GB; a release tree is a few hundred MB and only the last ones are kept |
| The cabinet's data and its nightly copies | about 0.5 GB, grows with the board |
| Each agent user: Claude Code and its caches | about 0.4 GB |
| The agents' folders, the repository copies, the worker slots | about 1 GB, grows with the projects |
| Memory service: its environment | about 2.5 GB; an upgrade keeps the previous one until it is removed |
| Memory service: its models | about 0.9 GB |
| Memory service: PostgreSQL | about 0.7 GB, grows with the memory |
| Component `transcribe` | about 1.5 GB |
| Component `e2e-runtime`, with Chromium | about 2.6 GB |
| Component `devcheck` | about 0.4 GB |
| Component `egress` | a few MB (tinyproxy) |

### Network

| Direction | To or from | What for | When |
|---|---|---|---|
| Inbound | 443 | The cabinet | Always |
| Inbound | 80 | The certificate (Let's Encrypt over HTTP) and the redirect to HTTPS | Always |
| Inbound | SSH | Whoever administers the host | Always |
| Outbound | Anthropic | Claude Code, its installer and its sessions | Always |
| Outbound | The forge, over SSH | The code, the overlay, the projects' repositories | Always |
| Outbound | The distribution's and Node.js's apt repositories, npm, PyPI | Packages | Install and updates |
| Outbound | The browsers' push services | Push notifications | Once someone subscribes |
| Outbound | The watched servers, over SSH | The sysadmin's reads and approved commands | With the sysadmin |
| Outbound | PyPI, the PyTorch index, huggingface.co | The memory service's environment and models | Its install |
| Outbound | The model's download | The transcription model | The component's install, once |
| Outbound | GitHub | The public mirror | Optional |
| Outbound | Telegram | The copy of critical alerts | Optional |

### Accounts and services

| What | What for | |
|---|---|---|
| A Claude Code subscription | One `claude setup-token` token of it (valid a year, the secret `claude-token`) signs in every agent, worker job and the memory service, which runs its model (`claude-haiku-5-5`) on the same subscription | Required |
| A Forgejo instance | The code repository and the deployment overlay (see [deployment-overlay.md](deployment-overlay.md)) | Required |
| A DNS name for the cabinet, pointing to the host | The cabinet's address; its TLS certificate comes from Let's Encrypt | Required |
| A GitHub repository | The public mirror | Optional |
| A Telegram bot | A copy of critical alerts | Optional |

## 1. Outside the host

These are yours to do once, in other systems:

1. **The overlay.** Create a private repository for the deployment. `python3 -I common/kd/overlay.py init <dir>`
   writes a starting overlay from this repository's examples (the `host/` files, the sysadmin's files and the closed
   scanner); fill in `host/host.json` (its `owner_language`, `en` or `ru`, is the language the owner reads), the sysadmin's inventory, `host/install.json` (which roles with how many
   session slots, which components, the pipeline's mode, whether the host runs the memory service),
   `host/projects.json` (the board's projects and their repositories) and the closed scanner's list
   `host/private_scan.json` with the deployment's own names and addresses. A release also needs one file no example
   can be: `common/install/forgejo_known_hosts` (the forge's SSH host key, checked against the forge itself; see
   [deployment-overlay.md](deployment-overlay.md)).
   `python3 -I common/kd/overlay.py check <dir>` says what is still wrong or missing.
2. **Forgejo.** Push this repository and the overlay; protect `main` on both: only the owner's account
   (`forge_owner`) pushes, only the owner and the merge account (`forge_merge_user`) merge, approvals count from the
   owner and the review account (`forge_review_user`). Create the developer agent's account (`forge_dev_user`) and
   the accounts above if your roles and components use them; their tokens come later, at the secrets phase.
3. **DNS.** Point `cabinet_host` to the host before the base phase: the certificate is requested there.

## 2. On the host

Clone the code and the overlay anywhere (for example `/root/src/`), then, as root:

```sh
bash <code>/common/install/bootstrap.sh all --plan --overlay <overlay> --code <code>   # what it would do
bash <code>/common/install/bootstrap.sh all --overlay <overlay> --code <code>
```

`--overlay` and `--code` are needed only until the first release tree exists; after it, every phase reads the
release tree (`/opt/digipaddock`) and the flags are ignored. `all` runs the phases in order and stops at the first
that does not finish; fix what it names and run the command it prints. One phase at a time:
`bootstrap.sh <phase>`. A role of `install.json` this code has no agent for, or names (`naming`) other than the
ones the host got at its first install, stop it before anything is installed.

| Phase | What it does | Where it stops for you |
|---|---|---|
| `preflight` | Distribution packages (`prereq.sh`), the overlay's check, root's key to the forge, read access to the release repositories | Node.js 22 and, with the memory service, PostgreSQL 18 with pgvector come from their vendors' apt repositories: the script prints the commands and never adds a foreign repository itself. Root's public key goes to both repositories as a read-only Deploy Key |
| `tree` | The first release tree: `kd-release --bootstrap` clones both repositories, composes the tree, checks it and switches `/opt/digipaddock` to it | — |
| `base` | The base user, the pinned Claude Code, the cabinet and its web, the release service `kd-release`, the session host (`stage_setup.sh`, `session_host_setup.sh`) | — |
| `hindsight` | The memory service: PostgreSQL role and database, the pinned `hindsight-api`, its env, units and nightly dump (`hindsight_setup.sh`) | — |
| `agents` | Each role of `install.json` under its own user, unless the installed one already is what this code installs (`new_agent.sh --verify`, then `--install`), then the board's projects of `projects.json`. On a kernel that restricts user namespaces (Ubuntu 24.04) the first agent also brings the `apparmor` package and the profile that lets the sandbox (`bwrap`) create them | A role that reads repositories (the analyst): the installer prints the key of the repository copies' user; it goes to every project's repository as a read-only Deploy Key. The sysadmin's servers come after its install: `bash agents/sysadmin/install/deploy_target.sh <alias>` for each, from the owner's machine (its SSH aliases are the inventory's): it puts the read-only gate on the server, pins the server's host key on the factory host and enables it. It also installs the exec gate there, in `observe` mode, with the owner's signing passkeys from the cabinet — so the cabinet's first owner needs a passkey enabled for signing first (`kd-cabinet user signing <id> --credential <prefix> on`, the prefix at least 8 characters of the key's id; the script prints the keys it installs, compare them with Settings → Devices). After the observation period: `deploy_target.sh <alias> --exec-mode enforce` server by server, and the envelopes flag (`/etc/digipaddock/exec-envelopes`) on the factory host, which makes the executors send signed envelopes |
| `components` | Each component of `install.json` (`review`, `repo-attach`, `transcribe`, `e2e-runtime`, `devcheck`, `mirror`, `egress`) as its installer makes it, and the pipeline's mode | `mirror`: the private part of a GitHub deploy key with write access, placed where the installer says |
| `secrets` | Checks the tokens the chosen roles and components need | Each missing one: `kd-secret put <name>` with where to get it |
| `keys` | Prints the public keys the host made and where each one goes (forge Deploy Keys, the developer account's SSH keys) | Always informational |
| `logins` | Each role on the session host with its slots (they sign in with the `claude-token` secret of `secrets`), then the `worker-slots` component (the developer's worker pool, where a pull request's council runs its critics beside the coordinator's jobs, and the analyst's, where a brief's council runs its) and `kd-review`'s signer | The signer: the cabinet's first owner (below) with a passkey, then `kd-review signers add` |
| `check` | Every check again from the release tree, the release tree's overlay (`overlay.py check`), the release's health (`kd-cabinet health`); installs nothing | Lists every failure |

Exit codes: 0 — the phase is done; 3 — a step is yours (the line, or the installer's lines above it, say which);
1 — an installer failed; 2 — usage. A Claude Code of the base user other than the pinned one is only a warning: a
new version is tried by hand (see Updating).

The first owner of the cabinet: `kd-cabinet user add <id> --name <name> --role owner`, then `kd-cabinet user code
<id>` prints a one-time code for the first sign-in; add a passkey in the cabinet's settings after it.

## A minimal start (optional)

An empty board says little about where to begin. For an owner who wants a hint, one more step, never part of `all`:

```sh
bash /opt/digipaddock/common/install/bootstrap.sh starter
```

It adds a project of the owner's, «Getting started» (`start`, route `@owner`), with a few cards in To Do, each with
items to tick: a tour of the cabinet, inviting a partner, and — on a host with those roles — the first task for the
analyst and the first server for the sysadmin. They are the owner's own cards: no agent is asked to do anything and
none of them runs a command or grants a right. The texts are in the first owner's cabinet language (English or
Russian), else in the cabinet's default; they live in `common/install/starter.json`. Run it after the first owner
exists. Running it again adds only what is missing; whatever the owner did with the cards since stays: when done
with the start, move its cards to Done.

## A host without some roles

`install.json` decides what is installed. A host without the developer agent leaves `dev` out of `roles` and the
components of its pull requests and checks out of `components` (`review`, `repo-attach`, `devcheck`,
`worker-slots`); the secrets and keys phases then ask only for what the chosen roles and components need. A
project's `route` and `done_by` must name roles of the host.

## With or without the memory service

`"hindsight"` of `install.json` decides whether the host runs Hindsight, the agents' long-term memory.

| | With Hindsight (`true`) | Without it (`false`) |
|---|---|---|
| What the agents remember | Their notes — shared (`memory/notes.md`, appended with `kd-notes add`) and one file per card or chat thread (`memory/tasks/`, `memory/chats/`) — and a memory bank each: they search it before answering about history and past decisions, and store finished cards, the owner's decisions and pitfalls in it. A project can have its own bank, shared by whoever works on its cards | Only their notes, carried from one session to the next. What a note does not keep, a new session does not know; the board's cards and their threads stay the record |
| The agents' instructions | A Memory section: when to search and when to store | No Memory section and no Hindsight tool named anywhere: the agent is never told to call what it does not have |
| The cabinet | Settings → Hindsight (status, banks, versions, spend) and a memory bank per project | The Hindsight page says the service is not installed; projects have no memory bank field |
| What the host needs | 8 GB of memory, 4 vCPU, 30 GB of disk, PostgreSQL with pgvector; its model calls spend the subscription | 2 GB of memory, 2 vCPU, 20 GB of disk |
| Install phases | `hindsight` installs the server, `agents` gives each agent the Hindsight plugin | `hindsight` does nothing; no plugin |

The mark of a host with the memory service is the server's env, `/etc/hindsight/coding-agent.env`, which
`hindsight_setup.sh install` writes: the plugin, the agents' instructions, the cabinet and its status collector all
read it. The env is written once, with its secrets; a setting the code's example (`common/hindsight/coding-agent.env.example`)
gained later reaches an installed host when root runs `hindsight_setup.sh install` after the release (`bootstrap.sh
check` lists it as missing): the settings the env lacks are appended, a value it already has is kept, and only then
do the server and its worker restart.

To add the memory service later: set `"hindsight": true` and release the overlay, then `bootstrap.sh preflight`
(it prints the commands for PostgreSQL with pgvector), `bootstrap.sh hindsight` and `bootstrap.sh agents`: the
agents' installed instructions no longer match, so each is reinstalled with the plugin and the Memory section.
Turning it off on a host that has it is not automated: `hindsight_setup.sh disable` stops its services and keeps
the env, the database and the agents' plugin.

## Updating

Every change reaches the host as a release of `main` of both repositories, never by copying files:

```sh
kd-cabinet release digipaddock                 # main of the code and the overlay
kd-cabinet release digipaddock --rev v1.2.0    # a signed tag (host/allowed_signers of the overlay) or a commit of main
kd-cabinet release digipaddock --rollback      # back to the previous tree
```

A release reinstalls the roles whose installed texts went stale and checks the release's health; the outcome is in
the cabinet's feed. After a release, `bootstrap.sh check` shows the whole host again. A new pinned version of
Claude Code (`common/install/versions.json`) is tried on the base user first, then on one agent, then on the rest.

## Tags and the GitHub release

A release tag `v…` is signed with the owner's SSH key; `kd-release` verifies it against `host/allowed_signers` of
the overlay. On GitHub the same tag builds a draft release: the tree with the built cabinet web, `SHA256SUMS` and a
keyless Sigstore signature (`.sig`, `.pem`) for whoever downloads the archive.

The mirror takes a tag to GitHub with its next run when the tag is on the forge's current `main` — pushed together
with that `main` or put on it after it went out. A tag on an older commit stays on the forge.
