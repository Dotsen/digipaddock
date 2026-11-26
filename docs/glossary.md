# Glossary

Terms used across the code, the instructions and these documents. Command names start with `kd-` (the host may set its own prefix).

**Acceptance criteria (AC).** Checkable conditions on a card that define "done". Agents check them off as they work. The analyst writes them before implementation starts.

**Activity journal.** A trusted log of verified agent actions per card, written by executors (`kd-work progress`), not by the model. The stall detector and the coordinator use it to tell real progress from a quiet session. See [ADR 0003](adr/0003-boss-worker-activity-source.md).

**Agent.** A persistent role installed from a manifest: its own Linux user, working directory, instructions, memory bank and chat in the cabinet. It is not one process or one conversation: it works in many sessions, one per thread (see Session), which the session host runs in its slots.

**Agent type.** What an agent is allowed to be: a profile of powers. Each persistent agent of a type has its own Linux user and its chat in the cabinet; its workers run under that user. The types are coordinator, analyst, developer and operator; a communicator type is foreseen. Kinds of work within a type are specializations. See [Team model](team-model.md).

**Agent state.** What the cabinet shows for an agent, derived from facts on the threads the reader may see: working (a session with a fresh heartbeat), waiting for you (its open questions), queued, free, error (the newest request of a thread failed and nothing runs), unavailable (the same, with the session host down or refusing) and unknown (not on the session host, or a session marked working that stopped beating). Every non-zero number behind it is shown with it.

**Analyst.** The agent that clarifies a task with the owner in a dialogue and writes a brief into the card; it reads repositories through read-only checkouts. See [Roles](roles.md#analyst-analyst).

**Aggregate card.** The parent of the cards an approved brief's stages created. The work is done on the stages: the parent shows how many are done, moves to Review by itself when all are Done, and a stage closed without its work is a hole in the plan, never Done. The parent of a chain plan gets a turn at the end instead: its agent leads the plan's pull requests — one per repository it changed — to the owner's decision and hands the plan in.

**App step.** A typed step of a sysadmin plan on a project environment of the catalog (`apps`): `app.tool` — the project's own tool with plain-word arguments, `app.flag` — one on/off switch of its config file. It runs as the application's own account in its directory; the signed plan carries its exact words.

**Approval service.** Retired: the owner's decisions moved into the card's thread in the cabinet (see Decision), and the cabinet service writes their grants.

**Attention event.** A record of something the coordinator must handle: a stalled card, a blocked card, a card waiting for the owner. The stall detector creates attention events; the coordinator acknowledges them when handled.

**Base user.** The host's main service user (`<prefix>`). It runs the executors that act on the board: the coordinator's, directives and wake-ups.

**Blocked.** Card status: the work waits for something. When the cause is a linked card (a dependency), the coordinator unblocks the card once the dependency is done.

**Board.** The shared task board: a SQLite database behind one service (the cabinet core), with the owner's web cabinet on top. All work lives on it as cards. A nightly Markdown snapshot in git mirrors it for reading.

**Brief.** The analyst's output on a card: goal, scope and out of scope, affected modules, approach, risks, acceptance criteria and size. It is a versioned document: the analyst's handoff records a version (the description and the criteria texts, with their hash), a person may save the current text as the next version, and starting the card approves exactly the version the person saw — an approved version never changes. A change of the text after it was ready makes the brief stale until it is a version again. Its state is derived, never stored: not requested, queued, being prepared, waiting for answers, ready, stale or failed.

**Cage (computed).** The part of a pull request that a merge alone does not put into effect: every changed file except `docs/`, `tasks/`, tests and plain `.md` files — instruction texts count whatever their extension: an agent's folder `agents/<name>/**` (its `CLAUDE.md`, playbooks and any text its `CLAUDE.md` includes), the shared template `common/agent-template/**`, the specializations and skills `common/specializations/**`, and a repository's `CLAUDE.md` and `.claude/` anywhere. `kd-pr` computes it from the diff and prints it with every pull request; the cabinet computes the same from the code host's file list, a renamed file by its old path too. A name with a control or an invisible character is shown quoted and escaped, as git quotes it, and is always the cage. A release renders the units and updates the cabinet itself, the factory reinstalls the generated agents and installs catalog components under the owner's signed plan; anything else in the cage the owner runs.

**Cabinet.** The owner's web application and PWA over the board, served by the cabinet service: the overview, the inbox, the board, cards with their thread, the agents and their chats, the feed, the servers and incidents, and the settings. People sign in with a passkey or a password and a one-time code, with a role (owner, partner, viewer) and the projects they may see. See [Cabinet](cabinet.md).

**Capability.** A named power from the finite registry `common/agent-capabilities/`, for example `board.read`, `board.write`, `board.coordinate`, `agent.nudge`, `git.develop`, `git.mirror`, `forge.review`, `repo.read`, `host.read`, `data.read`, `diag.request`, `e2e.request`, `factory.request`, `worker.request`, `servers.read`, `servers.execute`, `monitor.read`, `monitor.manage`, `docs.read`, `usage.read`, `net.egress` (a card session's probe exit, [probe-egress.md](probe-egress.md)). Each capability names its module, wrappers, executor, resources, sandbox ceiling and install steps.

**Capability channel.** The path of a privileged call. A Unix socket served by `kd-cap-serve` under the user who must run the command. The service identifies the caller by `SO_PEERCRED`, checks the request against the channel's permission file and runs the program. Channel `self` runs a wrapper as the agent itself, outside the sandbox. Executor channels (`exec`, `forge`, `base`) run as a service user.

**Card.** One unit of work on the board: title, description, acceptance criteria, notes, comments, assignee, project, status, dependencies.

**Chain plan.** A plan whose stages are all of one role — the agent that leads the plan — in one project with a connected repository, approved while the pipeline enforces and chain plans are not switched off (`"chain": false` in `/etc/digipaddock/pipeline.json`). Its stages run strictly one after another, on one branch and in one pull request of the parent card in every repository they commit into, without the owner's acceptance of each and with nothing merged before the end: a handed-in stage is read by a reviewer of the cabinet in every repository it changed (a read-only worker job on the stage's own commits there), and the coordinator — woken once, with the worst verdict — passes it, returns it on the reviewer's verdict (twice at most) or leaves it to the owner. The owner sees a stage only when it is held or the coordinator did not decide in 30 minutes, and decides at the end: on each of the plan's pull requests, after its council, and on the plan's acceptance. A plan with stages of several roles or projects is not a chain: each stage has its own pull request and acceptance. See [cabinet](cabinet.md#a-chain-plan).

**Check contour.** Where dev.checks run, from the release tree's contour file: the components it checks together, whether it is exclusive (one check at a time) or how many run at once, its state (idle, busy, maintenance). A contour goes into maintenance when the runner cannot run checks there (no token, a cleanup that failed) or by root's switch, and stays there until root releases it. The Servers page lists the contours.

**Claim.** Taking a card with `kd-task claim`: the executor moves a `To Do` card to `In Progress` and names the caller, under the board lock and a revision check. `kd-task` is the agents' only way to write to the board (claim, create in `Inbox`, edit own cards, handoff).

**Communicator** *(future)*. An agent type for agents that talk to people other than the owner on their own: customers, an audience.

**Continuation.** The next turn the board starts for an agent on its own card: after a turn that recorded `continue`, or when the waits a turn recorded are resolved (merged into the wake that resolved them). It holds only while the card is as it was — the same holder, in work, the same scope, review round and grant — and is checked again right before delivery. A window and a streak of identical turns limit it; at the limit it stops and the coordinator is asked.

**Coordinator.** The agent that runs the team (`boss`). There is exactly one per host.

**Council (of models).** The models that criticise a card's brief and its code before the owner reads them. Its composition — per kind the critics, the rounds and, for the code, the least diff — is set by a person with `settings` on a card or as a project's default; limits in `common/coordination/council.json`. The brief's council gathers at the analyst's hand-in: in each **round** every **critic** — a read-only worker job of the analyst's pool on the critic's model — reads that version of the brief and returns **findings** (`critical`, `major`, `minor`) and questions; the analyst answers each finding (`fixed`, `rejected`, `later`; only a critical or a major one without an answer holds the handoff) and a changed brief is the next round. The code's council gathers when the developer publishes a pull request of enough changed lines: every critic — a read-only worker job of the developer's pool — reads that head, the developer answers the findings, a new head is the next round, and the pull request's decision opens only when the council ends. It approves nothing: the owner starts the brief and decides on the pull request. See [cabinet](cabinet.md#the-council-of-models).

**Critical action.** A change on a server dangerous enough to need its own one-time approval of the exact command, on top of the task's plan approval.

**Dashboard chat.** Retired: what root services and executors told the owner there is now a notice in the cabinet. Telegram keeps only the duplicate of critical alerts.

**Decision.** Something the owner (or a person with the right) decides on a card, kept in its thread: a sysadmin plan or a critical command, a pull request, the coordinator's proposal to start a card or to split a goal, a brief, an agent's question. Risky decisions are signed with a passkey; the signed decision is what the executors check, never a chat message.

**Deferred card.** A card put off until a moment, no status of its own: its agent puts its own card in work off as the outcome of a turn (`kd-work finish --outcome wait_until --until <moment>`, up to 7 days), the owner any card not closed (“Put off until…”, up to 90 days), the coordinator any card not closed with a reason the owner is told (`kd-boss-board defer`, up to 7 days); the moment names its time zone and is at least 10 minutes away. Until then the card keeps its column with “⏰ until …”, its questions, decisions and Review stay out of the inbox and its badge, the idle and waiting reminders are silent (due and overdue are not), and an owner's or the coordinator's defer holds the agent's wakes from answers and closed waits. A new defer replaces the active one, but the agent's own never replaces an owner's or the coordinator's: that one stands, releases nothing, and the agent's moment wakes it no earlier than that defer's time. At the moment the scheduler (every 30 seconds, and on its first tick after a restart) wakes the card's agent once — or tells the owner when no agent holds it in work. A person's message to the agent and the owner's “Wake now” end it earlier; Done and Cancelled clear it. An agent's third defer in a row without `kd-work progress` raises `continuation_limit` for the coordinator.

**Delegation grant.** The owner's one-click permission for the coordinator to split one goal into subtasks and start them without further clicks, within limits: number of subtasks, how many run at once, projects, expiry. The owner can revoke it on the same decision in the cabinet.

**Device code.** A one-time sign-in code for a new device, valid 15 minutes. A person issues one for themselves in the cabinet's settings (confirmed with a passkey or a one-time code); the owner issues one for another person; root can issue one on the host.

**Denylist.** A hard list of forbidden server actions (for example, touching backups, the agents' own infrastructure, or kill switches). No approval overrides it.

**Dependency.** A structured link between cards: this card waits for that one. Used for ordering and handoffs, never for duplicates.

**Dev check (`dev.check`).** The required checks of a pull request's head, run by the factory: `kd-forge check` asks for one (the head, the target's head, the other components' heads, the passport's profiles — the same set answers the same check), the board queues it per check contour, a root runner checks the head out and runs each profile under the ci user with no network and no sockets, and writes the `devcheck/<profile>` statuses on the head. The developer's turn ends on it as a wait; its end — a class such as `passed`, `checks_failed`, `dependency_update_required` or `environment_not_ready` — wakes the card and is the snapshot's `checks:` line. With `kd-forge check … --publish` a green end publishes the pull request instead, and the wait goes on to the owner's decision (or the code council's round) without waking the developer.

**Diagnostic help.** A typed check of a platform service an agent's work depends on, in place of asking the owner to run a command: `kd-diag check <target> --card KD-<n>` probes a target of a root-owned catalog (one GET to a loopback health URL — the status line only, no redirect, no body — or a fixed read-only query of the board, such as the cabinet's push path), only for the card's single assignee while the card is approved, a limited number per card per hour, and records the result in the card's thread. A target the catalog lacks is `capability_missing`: platform work for the coordinator, not a question for the owner.

**Digest.** The daily summary to the owner: what was done yesterday, who is busy with what, what is stuck, what waits for the owner.

**Directive.** An instruction from the coordinator to an agent about a card: `work`, `clarify`, `status` or `pause`. Only a fixed line with the directive's ID reaches the agent, in the card's thread. The agent reads the text through `kd-work directive <id>` and answers with `kd-work reply`. A directive never adds rights.

**Effective policy.** Everything an agent may do, compiled from its manifest: Claude Code permissions and hooks, launch settings, sandbox, channel permission files, ACLs and install steps. Also called the generated policy.

**Executor.** A program that performs a privileged effect after validating every argument: merge a pull request, run a server command, write to the board for the coordinator. It runs under a service user and is reached through a capability channel.

**Factory operation.** A change of the host the factory runs from a fixed catalog under the owner's signed plan: `agent.reinstall` (drain a role's sessions with its queue kept, reinstall, check, resume) and `component.install` (a platform component in one of its fixed modes, on the deployed revision the signature names). The developer requests one with `kd-factory`; a root runner of the session host does it.

**Forgejo.** The code host. The developer opens pull requests there; the owner approves them with a signature in the cabinet (the approver service then posts the Approve) or with Approve in Forgejo.

**Gateway (read-only).** A forced-command program on every managed server (`agent-ro-gate`). It accepts only allowlisted read commands with checked arguments, runs them without a shell, and refuses secrets paths.

**Grant.** A trusted record of an owner's decision, written by the cabinet service when the decision is signed: a plan approval for a sysadmin task, a one-time critical action, or a delegation grant. Executors check grants; agents cannot create them.

**Help (assist).** One step a card's lead asks another role for inside its approved card (`kd-work assist request`) — a read, or, with kind `server_change`, the server steps of the lead's rollout, which the sysadmin plans on its own card for the owner to sign. The coordinator gives it a role of its kind without asking the owner; the helper answers on the lead's card with evidence or a reason, and the answer wakes the lead — only the answer, not the help being taken. A rollout answered with the sysadmin's plan card (`--card KD-<m>`) moves the lead's wait on to that card: its Done wakes the lead. Help never adds rights.

**Handoff.** Passing a part of a task that belongs to someone else as a linked card to the coordinator, who brings it to the owner or to the right agent. Also the executor operation `kd-task handoff`: the assignee and the `spec:` label of an own card change in one revision-checked write.

**Host prefix.** The short name that all host users derive from: `<prefix>` (base user), `<prefix>-<agent>` and the service users (`<prefix>-exec`, `<prefix>-forge`, `<prefix>-cabinet` and the others, see Service user). Set at deployment.

**Incident (P-n).** A problem the monitor found on a server, with a number, a level (warn or crit), a timeline and the cards that work on it. The cabinet's Servers section shows it; the owner acknowledges or mutes it there.

**Inbox.** Card status for work nobody has approved yet. Moving a card from `Inbox` to `To Do` is the owner's decision.

**Kill switch.** A single root action that stops one power or one agent at once, for example removing a channel's permission file or disabling an agent. Agents know the kill switches but cannot run them.

**Managed server.** A server the sysadmin watches and operates. It is reached only through the read-only gateway and the execution key.

**Manifest.** `agents/<name>/agent.json`: name, model, optional reasoning effort (`effort`) and thread models (`threads`, e.g. the «Monitor» thread's model), profile, capabilities, sandbox profile, working directory. Unknown fields are refused.

**Memory bank.** Long-term memory in Hindsight: a role bank per agent, and a project bank that the cards of a project share (see [Hindsight settings](hindsight-settings.md)). Agents write to it explicitly (decisions, pitfalls, finished cards), never raw transcripts.

**Monitor.** Scheduled checks without an LLM that detect problems on managed servers (services, disks, certificates, logins, ports, updates) and wake the sysadmin when something needs attention.

**Notice.** A message of a root service or an executor to the owner: a catalog key with parameters, posted to the card's thread (from `system:<source>`) or, without a card, the journal — a warn or critical one is a platform alert on the incidents page — with push by level. A critical one is also duplicated to Telegram as plain text.

**Nudge (wake-up).** A fixed line for an agent so it looks at the board, a card or a directive: a command in the board for an agent on the session host (in the card's thread, or in the agent's chat when there is no card), an agent that is not on the session host is not woken. Free text from one agent never reaches another this way.

**Next step.** The card's side panel: what moves the card on and who does it — a pipeline problem, the outcome its holder recorded and what it waits for (blocking and not), the coordinator's proposal to approve, an agent's question waiting for the reader, the run's state, the actions the reader may take, or in words why nothing is theirs to do (done, waiting for dependencies, waiting for the assignee).

**Operation catalog.** The sysadmin's list of typed server operations (restart a service, install exact package versions, upgrade the system without a reboot, one marked root cron line, one sysctl or logrotate file, the server's script switch and so on) that a plan may contain. The server runs a typed operation's own words, without a shell.

**Operator.** The agent type that operates servers; the `sysadmin` agent is one.

**Overview.** The cabinet's first screen, for one person and counted only from the cards they may read: what waits for them, the work under way, failures apart from normal waiting, the period's changes (what came after they last marked them seen is new) and the team's states, with a link to the Servers section.

**Owner.** The person in charge. Approves tasks, plans, critical actions and pull requests, and holds the kill switches.

**Panel.** An agent's terminal in the terminal multiplexer that hosted agent sessions before the session host. Removed with the multiplexer.

**Passport (project).** What a project's repository is for the developer and the factory: its clone, base branch, the target branches a pull request may go into, the required checks and their contour, whether a merge needs the branch current with its target, and whether a merge may release it. See [Architecture](architecture.md).

**Permission file.** `cap/<agent>/<channel>.json` under the host configuration directory, root-owned, generated from the effective policy. No file means the channel is off.

**Pipeline problem.** A record that a card's work stopped with nobody on it: a turn that owed an outcome ended without one, an outcome that could not apply, continuations stopped at their limit, a wait that cannot resolve by itself. Raised once per cause; in enforce it asks the coordinator (or, on the coordinator's own card, the owner) and is settled by the next fact that moves the card. The overview lists it as a stuck card.

**Plan.** The sysadmin's proposal for a card: which servers and which operations. The owner approves it once, signing its decision in the card's thread. Then each step runs without questions.

**Policy floor.** The managed Claude Code settings that apply to every session on the host. Generated policies cannot weaken them.

**Profile.** The registry entry behind an agent type (`analyst`, `coordinator`, `developer`, `sysadmin` for the operator type): its instruction defaults, the ceiling of capabilities it may select and its allowed sandbox profiles. A profile grants no powers by itself.

**Project.** A board grouping that also decides the default assignee through the routing table, for example development work to the developer and operations to the sysadmin.

**Promote.** Moving a card from `Inbox` to `To Do`. The coordinator can only propose it; the owner's approval of the proposal in the cabinet does it. The one exception is a subtask the coordinator starts under an active delegation grant, checked by the executor.

**Prompt pack.** A worker's view of a specialization: the registry text wrapped in the worker frame (unattended run, foreground commands, a structured result with the entry's keys) plus the chosen skill texts. Versioned and hashed; it adds knowledge and a way of working, never rights.

**Question form.** An agent's question to a person as up to eight typed questions — a single or a multiple choice with described options (one may be recommended, none is preselected) or a free text — with own words where allowed and a comment. The answer is kept both as a structure (the chosen options, the own words, the form's hash) and as readable text. An older plain-text question with options in its text is shown as a form recognized from it, with the original on request.

**Release.** Deploying a merged commit to the host: the release service unpacks it as a new release directory and switches a symlink atomically.

**Release health.** The checks a release runs after it switched: the session host, the web server, the cabinet over HTTP, the schema, the queues and the generated runtime. The result — healthy or degraded — is a notice in the thread of the card the release came from; a degraded release is a problem its lead holds until a healthy check, a later release or the card's close.

**Resource.** A named file or directory class that capabilities refer to (the board, the docs, a key). Canonical paths live in one root-owned table, so a manifest cannot open a new path.

**Revision.** A card's integer change counter. Writers pass back the one they saw, so that a card changed by someone else in the meantime is not overwritten (compare-and-swap).

**Rollout sheet.** The section of a pull request that says what must happen on servers after the merge for the change to work: per environment, the steps (app steps, a reload), the checks and the rollback — or that nothing is needed. The developer hands it to the sysadmin after a green deploy; nobody hands it to the owner as commands.

**Routing table.** The cabinet's projects table: every board project with its name and route — which agent (or the owner) a new card of the project goes to. The owner manages it in the cabinet; agents read it with `kd-task projects`.

**Sandbox.** The OS sandbox around every shell command of an agent (bubblewrap on Linux): which paths are readable and writable, and which network hosts are reachable. Selected by a sandbox profile from the registry.

**Service user.** A host user that is not an agent and runs one kind of executor, for example `<prefix>-exec` for server commands, `<prefix>-forge` for Forgejo, `<prefix>-cabinet` for the board and the cabinet, `<prefix>-review` for the approver of pull requests, and `<prefix>-repo`, `<prefix>-push`, `<prefix>-transcribe`, `<prefix>-observe`, `<prefix>-data` (the product databases' read key of `kd-data`), `<prefix>-ci`. The list is `SERVICE_ROLES` in `common/kd/naming.py`.

**Session.** One agent in one cabinet thread — a card's thread, its chat or its «Monitor» thread: a record in the board with the conversation's runtime id, fixed to that agent, thread and card by its first delivery. It outlives its processes: a session is run by at most one Claude process at a time, one turn at a time, and when the process closes (idle, a reinstall, a slot given up) the next turn starts a new process that resumes the same conversation by its runtime id — except in a «Monitor» thread, where a wake that finds no process starts a new conversation and reads what earlier wakes did as data. Two threads are two sessions with separate conversations, even of the same agent on related cards. The terms around it:
- *Claude process* — one run of `claude` the session host starts for a session in a transient unit under the agent's user; it lives for one or many turns.
- *Runtime id* — the Claude Code session id the process reports; the board keeps it and sends it with every delivery, and the next process starts with `--resume <id>`.
- *Slot* — the right to hold one Claude process: an agent has a fixed number (`session-host.json`), and a session without a process waits in the agent's queue for one.
- *Turn* and *worker job* — see their entries; a job runs in the agent's worker slots, a pool apart from its session slots.
See [Session lifecycle](architecture.md#session-lifecycle).

**Session host.** A root service that runs the agents listed in its configuration as headless Claude Code sessions under their own users, one session per card thread or agent chat, with a fixed number of slots per agent; the threads waiting for an agent's slots take one turn each, in turn. The board hands it commands and it reports turns and the agents' messages back. See Session.

**Skill.** A versioned Markdown instruction text containing reusable subject knowledge, selected from a specialization's allowlist. The runtime includes it in the model's prompt: persistent cards use `skill:<name>`, workers use `--skill <name>`, at most two at once. A skill does not create tools, worker slots or access rights. Source texts live in `common/specializations/skills/`; the registry declares versions, files and conflicts. See [Skills and specializations](skills-and-specializations.md).

**Snapshot.** What `kd-task view` prints: a card's operational state in at most 8 KiB — status and assignee, the next step and whose move it is (`recorded` when a pipeline problem or a recorded blocking wait says it, `derived` from the facts otherwise), the last turn outcome and the open problems, the newest request and its session, open and answered decisions, PRs and the last release, dependencies and children, the result so far and the newest significant messages (never the tool traffic), each cut part named by the exact command that reads it. The card's text, result, thread and single messages are read by pages of the same size (`kd-task scope`, `result`, `thread`, `message`); a page's cursor is refused once the document it reads has changed.

**Specialization.** A versioned instruction text bound to an agent type: purpose, inputs, procedure, output format and limits, for example `task-brief`, `test-author` or `post-deploy`. The persistent agent loads it as a playbook, a worker as a prompt pack. It also declares hard limits for workers (writes, capabilities, network), checked at launch. A card label `spec:<name>` selects it; results record the hash of its text. It never adds powers. The registry lives in `common/specializations/`; a persistent agent gets its type's texts as `playbooks/spec/<name>.md`, and taking a card records which one applies and the hash of its text.

**Stage (of a brief).** One part of an approved brief with its own role, specialization, dependencies and criteria. The owner's one approval of the brief creates a card per stage under an aggregate card. Stages of one role in one repository run as a chain plan; any other plan's stages each go through their own pull request and the owner's acceptance.

**Stall detector (watch).** A timer without an LLM that scans the board for stalled or blocked work, using rules in `common/coordination/watch.json`, and raises attention events.

**Telegram channel.** The Claude Code plugin that once connected an agent session to its own Telegram bot. Removed: agents talk to the owner only in the cabinet; Telegram keeps only the duplicate of critical alerts.

**Turn.** One run of an agent's session on one command: what started it, the agent's text and tool steps, the result and the cost. It runs in the session's current Claude process and is the unit of the agent's queue: a running turn is never cut, and between turns a session may give its slot to another thread. The card's Sessions tab shows a session's turns; a tool's output there is masked and cut.

**Turn outcome.** How a turn on an agent's own card in work ends, recorded with `kd-work finish` (the coordinator: `kd-boss-board finish`): `continue`, `wait_user`, `wait_dependency`, `wait_until`, `review` or `done`, with a one-line note for the owner. It stays pending until the turn ends; then the board applies it — a continuation, the waits, Review or Done within the closing policy — or voids it with a failed turn. A turn that asked for help and ended without an outcome is recorded by the board itself as waiting for that help, so the answer still wakes the lead. The pipeline's mode decides how much follows: observe records, enforce also starts continuations and asks about problems.

**Wait.** What a turn outcome waits for, as a record: an owner's decision on the card or a structured dependency (also a factory operation, help, a check contour, or — `wait_until` — a moment, see Deferred card). It resolves from its source in the transaction of the event that settles it (an answer, an approval, a dependency's Done); a set of waits wakes the agent once — when all of them, or with `--any` the first, are resolved. The board may carry a wait on to what comes next by itself (a help answered with a plan card, a check whose ordered publish opened the owner's decision): the thread says so (`wait.followed`) and the agent sleeps on. Wakes of one card that come while its agent's turn there runs, or within a short window of the owner's answers, reach the agent as one turn. Text such as “waiting” and the Blocked status are not waits. `kd-task waits KD-<n>` lists them.

**Watchdog.** Panel-era timers that restarted a dead panel and every agent once a night; removed with the panels. A session stops after ten idle minutes instead, so nothing important may live only in a session.

**Worker.** A short-lived Claude run of a persistent agent that the coordinator requests for one card and one specialization, in one of that agent's prepared worker slots and under its user. It is not a Session: it has no thread, no turns and no resume, and its conversation ends with the job. Its policy is the specialization's limits intersected with the agent's policy.

**Worker job.** One run of a worker: the coordinator's request on an approved card, kept on the board with finite states (queued, delivered, running, cancelling; done, failed, no-result, cancelled). The board hands it to the session host, which runs it in a free worker slot of its agent and reports its end: the result, a comment on the card on the agent's behalf, the usage, and a wake-up for the coordinator.

**Worker slot.** A prepared folder with its own clone where one worker job runs, in a transient unit the session host starts; an agent's worker slots are a pool separate from its own session slots.
