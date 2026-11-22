# ADR 0003: Worker activity for the 24-hour attention rule

- Date: 2026-10-07
- Status: accepted; the contract is live since KD-200 (2026-10-11) and was carried to the SQLite board on 2026-10-15 — see "Update" below
- Related: F05 / KD-122, B03, B05

## Finding

Backlog.md 1.53 `updated_date` is the time of a broad task edit, not the time of meaningful worker action. Its `updateTask()` compares fields including comments and implementation notes and updates the timestamp when they change. On an isolated stage board (`/tmp/kd-f05.oPymTC`), a BOSS coordination comment advanced `updatedAt` to 12:06 UTC; a worker implementation note advanced it to 12:08 UTC. Both look identical to a consumer of `updated_date`. A subsequent `kd.board.scan()` and a Git commit simulating technical sync left the task file's SHA-256 unchanged; `updatedAt` remained 12:08 UTC. Backlog comments expose `author` and `createdAt`, but `--comment-author @worker` accepted an arbitrary value on the test board. Implementation notes have neither per-entry timestamp nor authenticated author. File mtime, board Git commits, and `board/state.json` are also not worker-action evidence: they are affected by other writers or delayed sync.

Therefore the existing board, task history, and current audit trail cannot reliably implement “In Progress without meaningful worker action for 24 h”. A BOSS comment or technical sync must not postpone that timer.

## Contract

The detector receives validated worker-activity records, separate from Backlog task files. Schema v1 is represented by `common/kd/activity.py`:

```json
{"schemaVersion":1,"cardId":"KD-122","worker":"@dev","action":"progress","at":"2026-10-07T12:08:00Z","cardRevision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
```

`action` is one of `claim`, `progress`, `review_submission`, `artifact_link`; `at` is UTC with second precision. `cardRevision` is the SHA-256 of the task file observed by the trusted producer after the action; it is evidence, not a key that must match the latest revision after unrelated edits. No comment body or secret enters the journal. The B03 selector matches the current card and assignee. Reassignment does not inherit the former worker's timer. Missing, malformed, truncated, or untrusted history means **unknown**, never “24 hours since `updated_date`”. B03 must suppress this one rule and report a sanitary data-quality diagnostic; other attention rules continue.

The production journal should be a bounded, versioned, append-only root-owned file outside the writable board, e.g. `/var/lib/digipaddock/board/activity.jsonl`. Only a trusted service/wrapper may append a receipt **after** it has verified the actor and successful meaningful operation. The producer must enforce the finite action list and evidence, reject empty “keepalive” writes, serialize writes under a lock, and `fsync` before acknowledging. BOSS has read access at most and no write route. A caller-provided `comment-author`, note prefix, or `worker` JSON field is never authentication. Legacy agents sharing one Unix uid cannot be attributed to an agent solely by uid; for their cards the rule stays disabled until a trustworthy per-agent producer exists. Existing cards cannot be backfilled from `updated_date`.

`common/kd/activity.py` is intentionally a pure schema/selection function, not a privileged writer. B03 may consume it in tests; B05 must not enable the 24-hour production rule until the producer and its access-control/integrity tests are deployed. This gate prevents a false confidence in activity data while keeping the rest of the detector available.

## Implementation (B05, 2026-10-08)

`common/kd/activity_journal.py` is the producer and reader of `/var/lib/digipaddock/board/activity.jsonl`: attribution by the caller's Unix user, which must be the worker's own `agent-<role>` user (the shared `agent` user is refused); the producer reads the card itself and requires the worker to be its single assignee; the evidence is the card file's SHA-256 and a repeat with the same evidence is refused as a keepalive; one record per line under `flock`, `fsync`, size-bounded with the newest lines kept. No trusted wrapper calls it yet: hooking it into dev's wrappers changes a live agent and belongs to M01. Until then `trustedWorkers` in `watch.json` stays empty and the 24-hour rule stays unknown.

## Update (2026-10-11, 2026-10-15)

- KD-200: the trusted producer is the executor behind `kd-work progress`, called through the agent's capability channel (the caller is the agent's own user, verified by the channel); `trustedWorkers` lists `@dev` and `@sysadmin`, and the 24-hour rule is on.
- KD-225 (ADR 0004): the board is a SQLite database behind `kd-cabinet`. `cardRevision` is now the card's integer revision (the cabinet's change counter) that the producer saw, not a file SHA-256; the keepalive check compares that number. The rest of the contract — finite actions, single assignee, no backfill, unknown on bad history — is unchanged. The "Finding" above describes Backlog.md and is kept as the record of why the journal exists.

## Verification

- Isolated stage board: BOSS comment changed `updatedAt`, worker note changed it again, and read-only scan plus Git sync preserved the task file hash and `updatedAt`.
- Same caller could set `--comment-author @worker`; comment attribution is not a security boundary.
- `common/tests/test_activity.py` checks the exact schema, assignee/card selection, unknown state, and that BOSS comment/sync timestamps do not reset the worker clock.

Source: [Backlog.md v1.53.0 `updateTask()`](https://github.com/MrLesk/Backlog.md/blob/v1.53.0/src/core/backlog.ts).

## KD-200 (2026-10-11): the producer is wired

The journal moves to `/var/log/digipaddock-board/activity.jsonl`, a 0640 file of the base user next to the other board journals. The producer runs there: workers call `kd-work progress KD-<n>`, which reaches the base user's executor through the worker's `base` capability channel; the channel, not the caller, names the worker (`KD_AGENT`, and `KD_CALLER_USER` from `SO_PEERCRED`), and `record()` still requires that user to be the worker's own registered user and the worker to be the card's single assignee. `action="auto"` derives the action from the evidence: the worker's first record on the card is `claim`, a card in Review is `review_submission`, anything else `progress`. `kd-board watch` (root) trusts the journal only as a regular file of the base user, like the ack journal; any other file is untrusted and the 24-hour rule stays unknown. `trustedWorkers` is switched on for `@dev` and `@sysadmin` only after the stage probe (KD-200 spec §7). The probe passed on 11.10.2026 (an internal stage protocol of the first deployment): `trustedWorkers` = `@dev`, `@sysadmin`.

Update (2026-11-07): `@analyst` was added later; the current list is `trustedWorkers` in `common/coordination/watch.json`.
