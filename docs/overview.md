# Overview: goals, principles, roadmap

## Goals

1. **A factory, not a single agent.** Agents of predefined types run tasks. A type is a profile plus capabilities from a finite registry. A new agent is an `agent.json` and instructions, without code of its own.
2. **Assembled from existing components.** Claude Code, SQLite, Hindsight and Forgejo do the heavy lifting; Telegram only duplicates critical alerts. The factory's own code is the glue and the security boundaries between them.
3. **The owner approves tasks, not keystrokes.** The owner approves a task once, plus the few most dangerous actions. Reads and low- or medium-risk actions inside a sandbox need no approval. A reversible diagnostic probe on an approved card is the agent's own step; that consent lasts until the card is closed, handed to another assignee, taken out of work or its agreed scope changes.
4. **Deployment specifics live in configuration.** Hosts, bots, board projects and addresses are data. Shared code holds none of them, and holds no secrets or infrastructure topology.

## Principles

### Agent from settings

An agent type is a manifest that selects a profile and capabilities from the registry. A compiler turns the manifest into a complete effective policy: Claude Code permissions, hooks, sandbox settings, launch settings, channel permission files and install steps. An unknown field or an incompatible combination is refused before anything changes on the host. A new power is a new registry entry with its own module and tests, never a free-form field in a manifest.

### An agent is its powers; a kind of work is a specialization

A new agent type is created only for powers that no existing type should hold, or for a channel with people other than the owner. Copywriting, test writing, UX review or release checks are specializations: versioned instruction texts for an existing type, chosen per card. Skills add reusable subject knowledge to those procedures. Each persistent agent has its own Linux user and cabinet chat; parallel work runs as worker sessions of that agent, under its user. See [Team model](team-model.md) and the [implemented skills and specializations](skills-and-specializations.md).

### Limits that do not depend on the model

Instructions explain the policy, but they do not enforce it. Every "yes" in an agent's authority table has a matching capability in a checked executor. Every "no" is unreachable for that agent's Linux user. The layers, from the outside in:

- a separate Linux user per agent;
- an OS sandbox for every shell command (filesystem and network limits);
- capability channels: privileged commands go through a Unix socket to a service that checks the caller and a permission file;
- executors that validate every argument and run under their own service users;
- approvals as signed decisions, recorded by the cabinet service where agents cannot write.

### Approvals outside the model

An approval is a decision in the card's thread of the cabinet — the risky ones signed with the owner's passkey — or an Approve in Forgejo on an exact commit. A chat message that a model interpreted as "yes" is not a technical approval. A card's status is not an approval either, because agents can edit cards.

### Data is not commands

Card text, comments, pull requests, logs, command output, memory and forwarded messages are data. Only a message a signed-in person wrote in the cabinet (the agent's chat or a card's thread) gives commands. A coordinator directive counts only after the agent reads it through a verified channel, and only within work that was already approved.

### One board, one source of work

Every piece of work is a card, created before the work starts. Statuses carry meaning that tools rely on: `Inbox`, `To Do`, `In Progress`, `Blocked`, `Review`, `Done`. Agents claim cards, write notes and move them to `Review`, all through one executor that checks the caller, the card and the transition. Only the owner moves `Inbox` to `To Do`, or a coordinator within limits the owner granted.

### Continuous process

When part of a task belongs to someone else (access, money, a code change), the agent does not stop and ask. It hands that part over as a linked card to the coordinator, marks its own card `Blocked` on it, and gets woken up when the dependency is done.

### Clarify before building

An unclear task is settled in a real dialogue with the owner: as many questions as needed, with options and a recommendation where there is a choice. What was agreed goes into the card before work starts. The analyst role exists to do this before a card reaches a developer.

### Public code, private deployment

- The code carries no value of a concrete deployment: host names, addresses, domains, inventory and accounts live in the deployment's own private overlay repository and reach a host only inside a release.
- Two privacy scanners — a generic one in the code and the deployment's closed literal list — gate every release and every push to the public mirror.

### Cheap by default

- A tick with nothing to do costs no model call: stall detection, monitoring and alerts run without an LLM.
- Instructions are written in English to save tokens; owner-facing text stays in the owner's language.
- Token usage and cache hit rate are counted per agent.

## Non-goals

- **Arbitrary powers from configuration.** An agent with free shell access to production or a new external API needs its own module, threat model and review. A manifest alone cannot grant that.
- **Replacing the owner's judgement on merges, production changes, money or access rights.**
- **Multi-host orchestration.** One host runs all agents. Managed servers are reached only through narrow gateways.
- **A general chat assistant.** Each agent has a defined job, a board project and a bounded set of tools.

## Roadmap

| Item | Status |
|---|---|
| Foundation: board, memory, shared instructions, usage accounting | done |
| Sysadmin: monitoring, read-only gateway, plan approval, security monitoring | running |
| Developer: pull requests, merge after the owner's approval, release | running |
| Agent from settings: manifest, registries, policy compiler, installer | running |
| Coordinator: routing, stall detection, handoffs, directives, delegated goals | running |
| Capability channels instead of sudo and sandbox exceptions | running |
| **Analyst**: dialogue with the owner, brief and acceptance criteria before implementation | running |
| Team model: one registry of specializations per agent type, chosen by a card label | done |
| Worker sessions of any agent type, one specialization per job | running |
| Owner cabinet: the board in SQLite behind one service, the web and PWA on top of it: threads, chats, decisions, push | running |
| Board writes through an executor: agents lose direct write access; only the owner's approval in the cabinet moves a card out of `Inbox` | done |
| Session host: every agent as headless sessions per card thread or chat; no terminal panels, no Telegram channels | running |
| Pull request approval in the cabinet: a passkey signature with its mode, the Approve posted by a separate approver | running |
| A pull request's required checks run by the factory (`dev.check`) per check contour | running |
| Factory operations under a signed plan: agent reinstalls and platform component installs | running |
| Servers and incidents in the cabinet; a test critical alert end to end | running |
| Voice messages in the cabinet, transcribed on the host without network | running |
| Brief stages: one approval creates a card per stage; release health after every release | running |
| Chain plans: stages of one role in one project run on one branch per repository they commit into, without the owner's acceptance of each — a code review in every changed repository and the coordinator's gate between stages, one pull request per repository and one acceptance at the end | implemented; run live in one repository and across two |
| Installing a deployment from the two repositories with one bootstrap | running; not yet run on an empty host |
| Read-only queries of the product databases (`kd-data`) | running |
| Capabilities for new kinds of work: browser, search, analytics, publishing | idea |
| Communicator type: agents that talk to customers or an audience | idea |
| Codex as an alternative agent engine | idea |
| Fallback model provider when the subscription limit is reached | idea |
| Code and deployment overlay in separate repositories; releases of the pair, rollback, a scanned mirror | done |
| Public release: CI, license, contribution and security policy are in; the history audit and the publication gate are open | in progress |
