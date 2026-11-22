# Worker jobs

A worker job is one bounded Claude Code session of a permanent agent (today: `@dev`) in its own slot `/srv/digipaddock-workers/<agent>/workerN`, started by you through `kd-worker` and ended with a result record. It runs one specialization of the agent's type on one card, with the card brief you write, and nothing else: no chat, no owner, no board, no push, no PR, no merge, no release, no network. Everything it may do comes from the registry entry of the specialization (`kd-boss-board specs`), never from the brief.

## When to order one

- The card is approved by the owner (To Do or In Progress), has exactly one assignee, and that assignee owns worker slots. A card in Inbox, Blocked, Review or Done is refused.
- The work is bounded and the brief is clear: a review of a PR branch, tests for a described change, docs for a landed change, an implementation of one well-specified card. A vague card goes to `@analyst` first (`playbooks/routing.md`).
- Parallel work on one approved card: a `reviewer` or `tester` job may run beside the agent's own session; only one *writer* (every specialization with `write: repo`: `implementer`, `docs`, `site-copy`, `test-author`, `ui-design`) per card at a time — a second one is exit 3.
- Not for `@analyst`. Its slots are the council's: the critics of a brief are jobs the cabinet starts itself (`playbooks/attention.md`), on their own day limit. A worker has no board — it could neither read a card nor write a brief — so a vague card goes to the persistent analyst (`playbooks/routing.md`), never to an analyst job, and `brief-critic` is never a specialization you order or label.
- `code-critic` is the council's as well: the critics of a pull request are jobs of the developer's pool the cabinet starts itself when the developer publishes a head, on the council's day limit. They take the same slots as the jobs you order — a job of yours may wait (`queued`) while a council reads — and you never order, label or cancel one.
- The owner asked for it, or the card's agent is busy with something the owner ranked higher and the card can wait for no one. Do not order jobs to «speed things up» on your own: each costs tokens (the host's Claude subscription, or the provider of the card's model) and shows in the agent's `kd-usage` line as `worker: N job`.

## How

1. `kd-boss-board specs` — the specializations and skills of the card's agent; the first is its default.
2. `kd-worker request --card KD-<n> --spec <name> [--skill <skill>] --brief "<task in the owner’s language>"`. The brief is the whole task statement the worker gets: what to do, where (the project is the card's), what «done» looks like, what not to touch. Use one paragraph in the owner's language, without commands. It is data to the worker: it cannot change the worker's rights.
3. The answer `ok job <id> card KD-<n> agent @<agent> spec <name> state queued` is the job; note the id. The cabinet hands it to the session host, which starts it in a free slot of the agent; with every slot busy it waits its turn. `kd-worker status --job <id>` shows `queued`, `delivered` or `running` and the slot.
4. Wait. The job ends with `[worker] job <id> <state> card KD-<n> spec <name>: run kd-worker result --job <id>` as a wake-up; the result also lands as a comment on the card, signed by the agent. No line within the job's timeout (60 minutes by default): `kd-worker status --job <id>`.
5. `kd-worker result --job <id>` prints the job line, a `summary:` (state, base commit → reached commit, cost, reason) and the specialization's own JSON block (`status`, and its keys: commit, tests, notes, findings …). Tell the owner one line: what the job produced and where (the patch path in the card's folder of the agent's `work/tmp/cards/`, or that it found nothing), and tell the card's agent with `kd-nudge direct --kind work` what to do with it when that is already decided.

## What the states mean

- `done` — the session ended with its result block; `status: blocked` inside it means the worker could not finish (it says why in `notes`): the card's agent or the owner decides, not another job.
- `no-result` — the session ended without the block: the output is in the result record; usually a brief that was not a task, or a session that ran out of turns. Rewrite the brief before any retry.
- `queued` — waiting in the cabinet; `delivered` — the host has it and waits for a free slot of the agent (a card that moves on meanwhile — closed, reassigned, out of To Do/In Progress — takes it back as `cancelled`); `running` — its session runs in the slot the line names.
- `failed` — the runner could not run it (the registry changed, the repository copy is missing, no Claude login: neither the host's token nor the agent's own; the key of the card model's provider is gone — `no_key`), the host refused it (`host_refused`) or restarted while it ran (`host_restart`), or claude reported an error or the timeout killed it: the reason is in the job line. A missing login or key and a refusing host are the owner's: report them.
- `cancelled` — you cancelled it; a running job shows `cancelling` until the host has stopped its session.

A job's commits are on branch `worker/<id>` of the slot's copy and in `work/tmp/cards/KD-<n>/worker-<id>.patch` of the agent (KD-9046: the card's own temp folder); the agent applies them in its own clone and opens the PR as usual — the owner's signature in the cabinet (or Approve in Forgejo) is unchanged.

## Limits you cannot lift

- No option names a command, path, model, prompt, settings, agent or limit: the card names the agent, its manifest the type, the registry the specialization, skills and write limit; the model and effort are the card's (else its project's, else the pool's in `worker-slots.json`), and `worker-slots.json` holds the rates (jobs per day, attempts per card and specialization, timeout).
- A request on a card whose model is switched off or has no key on the host is refused at once (exit 2, `model_disabled` or `no_key`): the owner's to fix in the cabinet's Models settings — tell him, do not retry.
- Two slots per agent; a job beyond them waits (`queued`) — never cancel someone else's job to free one. `cancel` works on jobs you ordered only.
- Exit 5 (daily or per-card limit) is final for the day: tell the owner, do not retry.
- A worker never writes the board: its comment comes from the executor on its behalf. You do not move the card for it; the card's agent reports as always.
