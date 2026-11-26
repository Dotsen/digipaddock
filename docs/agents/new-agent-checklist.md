# New agent checklist

An agent is a manifest, instructions and a choice of existing capabilities — no code of its own. The model behind it is in [Team model](../team-model.md) and [Architecture](../architecture.md#agent-from-settings). Steps marked **owner** need the owner; everything else goes through a reviewed change.

## Before you start

- Every right comes from a capability in `common/agent-capabilities/`. A command that leaves the sandbox runs through a capability channel (`kd-cap run <key> …`, a socket served by `kd-cap-serve` as the executor's user), never through sudo or `sandbox.excludedCommands`. If the role needs a new trust boundary or external effect (an executor, a secret, an API, a network domain, a new kind of board change), stop: that is a new module with its own tests and its own design, not a manifest field.
- The agent gets its own Linux user `<prefix>-<name>` (`digi-<name>` with the default prefix `digi`), derived by `common/kd/naming.py` from the host prefix in `/etc/digipaddock/naming.json`; the manifest names no user. One user per agent: a second agent of the same type gets a user of its own. The base user (`digi`) owns the board's executors (`kd-task`, `kd-work`); the names of the service users and the reserved ones (`RESERVED_AGENTS` in `common/kd/naming.py`: `exec`, `forge`, `cabinet`, `review` and the rest, `ro`, `worker`) are refused as agent names.
- `sysadmin`, `dev`, `boss` and `analyst` are installed by `new_agent.sh` like any new agent; there is no other launch path. Read their manifests for examples, not their host files.

## Steps

1. **Card and role.** A card on the board (project `digipaddock`) and a one- or two-page role spec: why the agent exists, what it does, what it never does, which capabilities it needs and why.
2. **Manifest and instructions** in `agents/<name>/`, through a PR with the owner's approval:
   - `agent.json` (`schema_version` as `common/kd/manifest.py` expects; KD-180 dropped `user`): `name` equal to the folder, optional `model`, optional `effort` (`low` … `max`) and `threads` (a model for the «Monitor» thread), `profile` from `common/agent-profiles/`, `capabilities` within that profile's ceiling, `runtime.sandbox_profile` allowed by the profile, `working_directory: "work"`. Unknown fields fail closed.
   - `CLAUDE.md` with the role part only (in English); the common sections are rendered from `common/agent-template/CLAUDE.common.md` by the installer.
   - `playbooks/*.md` if the role needs them.
3. **Routing.** If the agent takes cards of a board project, the owner creates the project (or reroutes an existing one) to the agent in the cabinet; the projects are the cabinet's table, not a repository file (`kd-task projects` lists them).
4. **Tests before review.** The checks [testing](../testing.md) selects for the change — a new agent touches the permission boundary, so `python scripts/check.py --profile full --scratch-root /tmp --base <branch point>`. Manifest and registry errors show up here, before anything reaches a host; a project routed to an agent the repository lacks is refused by the install check (step 5), which reads the projects from the cabinet.
5. **Check and plan (owner, on the host, from the deployed code).**
   - `new_agent.sh --check <name>`: manifest, the board's routes (asked from the running `digipaddock-cabinet`: install the cabinet before the first agent; without it the check stops with «start digipaddock-cabinet first»), compiled policy, module files, user conflicts, dependencies. It changes nothing and must print no problem.
   - `new_agent.sh --plan <name>`: every action, file, ACL, capability channel (its run-as user, commands and grants file) and unit of the install. Read it: the rights listed there are the agent's effective rights.
6. **Install (owner, root).** For `repo.read`, run `common/install/repo_setup.sh install <name> <user>` first: it creates the repository service user with its own read-only deploy key, prints the public key for the forge and checks every registered repository before the first install can grant the checkouts. Then `new_agent.sh --install <name>`. The install is idempotent: a second run with nothing changed reports `unchanged`. Then the owner steps the plan printed, in order:
   - nothing to log in: the host's Claude token (`kd-secret put claude-token`) signs in every agent; a host without it needs `/login` once as the agent's user (`sudo -iu <user> env DISABLE_AUTOUPDATER=1 /opt/digipaddock-claude/claude`: root's pinned binary, the variable keeps it from updating itself);
   - put the agent on the session host: `session_host_setup.sh enable <name>`. Agents have no panel and no Telegram channel: the agent talks to the owner in the cabinet only, in card threads and its own chat.
7. **Smoke test.** A card in `To Do` for the agent gets an answer in its thread, and a message in its chat in the cabinet gets one there; each granted capability works once; the neighbouring capabilities, other agents' homes, secrets, the network outside the sandbox allowlist and forbidden board transitions are refused; `kd-usage show` lists the agent.
8. **Record.** A status line in the deployment's overlay `CLAUDE.md`, the role in [Roles](../roles.md) and, for a new kind of agent, a row in the [roadmap](../overview.md#roadmap); close the card.

## Turning an agent off

- `new_agent.sh --disable <name>` takes an agent off the session host first (its queued commands are cancelled), then stops the agent, removes its channels (grants files, units, `agent-bin` links) and ACL first and invalidates its generated policy. One channel alone: `rm /etc/digipaddock/cap/<name>/<key>.json` (the next call is refused); all channels of all agents: `systemctl stop digipaddock-cap.target`. Home, Claude login and memory stay; there is no data removal.
- Deleting data (`--purge`) is a separate, explicit owner operation and is not part of this installer.
