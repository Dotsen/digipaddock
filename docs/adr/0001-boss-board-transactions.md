# ADR 0001: BOSS board writes must join Backlog.md's task transaction

Date: 2026-10-07
Status: superseded by [ADR 0004](0004-own-board-in-sqlite.md) (KD-225: the board is the cabinet's SQLite store; the
Backlog.md fork and `board_tx` are removed). Before that: accepted; transaction adapter implemented in B00 (KD-138, see
"Implementation")

Note (2026-11-07): paths, units and user names below were rewritten mechanically by the 30.10.2026 rename; they
describe the removed Backlog.md board and never existed under these names on a host.

## Context

The stage board uses Backlog.md **1.53.0** (`backlog --version`) at
`/srv/digipaddock-agents/board`. Its `backlog/config.yml` has
`filesystem_only: true`, `auto_commit: false`, and six statuses including
`Inbox` and `Blocked`. A card is one UTF-8 Markdown file with YAML frontmatter
under `backlog/tasks/` (for example `kd-1 - race-test.md`). The file contains
identity/status/assignee/date fields and section-marked prose; a change of
title may also change the filename. The SHA-256 of the **current complete file**
is the proposed BOSS revision, not `updated_date` or the delayed
`board/state.json` snapshot.

### Writer inventory

| Actor / entry point | Actual task mutation path | Coordination today |
|---|---|---|
| Owner via `kd-board run -- task ...` | `common/bin/kd-board` runs the installed CLI as `agent` | Backlog.md's task/create locks |
| Owner via browser/dashboard | `digipaddock-board-web.service` runs Backlog.md browser as `agent`; web updates use Backlog core | Same task lock as CLI |
| `sysadmin`, `dev`, `probe-agent` | Their instructions invoke `backlog task create/edit`; launch sets `BACKLOG_CWD` | Same task/create locks |
| `kd-task claim` | `common/bin/kd-task` does `view`, then CLI `edit` | An additional `fcntl.flock` on `kd-claim.lock` serializes **claims only**; ordinary CLI/web edits do not take this lock |
| Future BOSS | Planned `kd-boss-board` finite commands | Not implemented; cannot safely use a separate BOSS-only lock |

`common/kd/board.py` sync/digest and the sysadmin monitor read task files but
do not write them; board sync writes a Git snapshot and its own state file.
The setup script initializes/configures the board, not normal task edits.
Root and anyone with filesystem write access could bypass the CLI; the runtime
contract must prohibit direct task-file writes and verify effective ACLs before
enabling BOSS.

In [Backlog.md 1.53.0 `operations.ts`](https://github.com/MrLesk/Backlog.md/blob/v1.53.0/src/file-system/operations.ts),
task `edit` takes a `proper-lockfile` lock at
`backlog/.locks/task-<lowercase-id>` with no retry; `create` has a separate
`backlog/.locks/create` lock on this filesystem-only board. The edit path
re-reads the task while holding its lock. CLI and web share that core path.
However, `saveTask` calls `Bun.write(filepath, content)` on the task file:
this is not a temporary-file-plus-rename transaction for unlocked readers.
`task edit --help` exposes no expected-revision/CAS parameter. The lock can
also be disabled by `USE_GLOBAL_TASK_ID_LOCK=false`; the stage launch policy
must disallow that override.

### Stage reproduction, isolated board only

On 2026-10-07 we initialized `/tmp/kd-f01.6GuebF` with `--no-git
--integration-mode none --task-prefix KD` and created test task `KD-1`.
The live `/srv/digipaddock-agents/board` was not used for these experiments.

1. Two independent SSH/CLI writers appended `A<i>` and `B<i>` to the same
   task concurrently for 12 rounds. In round 9, writer A exited 1 with
   `Edit failed: KD-1 is being modified by another process; retry if appropriate.`
   Writer B succeeded. The other 23 edits succeeded; successful append
   markers were present in the final file. This demonstrates contention on
   Backlog's lock, **not** a guarantee that an external read/edit sequence is
   atomic.
2. Deterministic lock probe: while the test-only directory
   `backlog/.locks/task-kd-1` existed, `backlog task edit KD-1
   --append-notes lock-contender --plain` failed with the same message. The
   directory was then removed.
3. Stale-read interleaving: writer A read SHA-256
   `ff333ced66f9815f056bb9c4d7996b16799b0e659a7b9e5a28b00b3aedb6d0e1`.
   Writer B appended a note (new hash `af7d757818b85245c824e449aa7effed2b30f6a3840d608f856bfecb58f2c58d`).
   Writer A then edited using its stale decision; plain `task edit` returned
   success (new hash `f53d4c8ac3d4a41ab122960ed02c0d51e7804c162d203fb9b3a438434d28cd20`).
   Both append markers survived because Backlog re-read under its lock, but
   it did **not** reject A's stale decision. An assignment, dependency, or
   unblock based on the old snapshot could therefore be semantically wrong.

Reproduction recipe (use a fresh test directory, never the shared board):

```sh
d=$(mktemp -d /tmp/kd-f01.XXXXXX)
BACKLOG_CWD="$d" backlog init 'F01 race' --defaults --no-git --integration-mode none --task-prefix KD
BACKLOG_CWD="$d" backlog task create 'race test' -s 'To Do' --plain
# In two shells: run `BACKLOG_CWD="$d" backlog task edit KD-1 --append-notes A --plain`
# and the same command with B at the same time; repeat to observe lock contention.
mkdir "$d/backlog/.locks/task-kd-1"
BACKLOG_CWD="$d" backlog task edit KD-1 --append-notes contender --plain  # must fail
rmdir "$d/backlog/.locks/task-kd-1"
# For stale-read: hash the task file; let B edit; let A edit based on the old hash.
```

## Decision and required change

Do **not** implement BOSS CAS as `hash file → backlog task edit`, and do not
add a BOSS-only `flock`. Neither covers a CLI/web writer between check and
mutation; `kd-claim.lock` has the same limitation.

Use a **version-pinned Backlog transaction adapter** for BOSS commands:

1. Extend or wrap the installed Backlog.md 1.53.0 core update path, not a
   second Markdown serializer. Inside `withTaskLock(taskId)`, resolve and
   re-read the current regular task file, compute its byte SHA-256, compare
   with BOSS's expected revision, validate the finite operation against that
   fresh task, then call Backlog's normal update/serialization **before
   releasing the same lock**. Mismatch is a typed conflict with no mutation.
2. `create-inbox` must use Backlog's existing create-ID lock and fixed status.
   It does not accept a fabricated task revision; return the created ID and
   initial file revision. For `unblock`, re-read the structured dependency
   under a defined consistency rule; a dependency's state may change between
   checking it and writing the blocked card, so lock both cards in
   deterministic ID order through the same Backlog lock mechanism. Reject
   or retry on either revision mismatch; never update from an unlocked read.
3. Make the file write atomic for readers (same-directory temporary file,
   flush/fsync, replace, parent-directory fsync on Linux) in the pinned core
   before relying on file hashes or `board.py` scan; preserve file ownership,
   mode, naming/rename and callback behavior. This is a separate compatibility
   change requiring Backlog CLI/web regression tests. A successful write must
   expose either the old or new complete file, never partial Markdown.
4. Keep all existing CLI/web/`kd-task` task writes on that same core lock.
   Deny raw `backlog` and direct board write to BOSS by effective ACL/sudoers,
   not merely prompt text. Pin and verify binary/source version; upgrades
   must re-run the race, CAS, file-integrity and callback tests.

Alternative: funnel **every** writer, including web and owner CLI, through a
single trusted board service with a shared transaction. This is a larger
migration and is not implicitly authorized by B01.

## Consequences / gate

**B01 may define pure command validation but may not ship or enable a board
mutation path** until the adapter and atomic-file behavior above are
implemented, installed in an isolated stage test board, and proven against
CLI, web, `kd-task`, and BOSS concurrency. Required negative tests: stale
revision, two simultaneous edits, create-ID collision, direct-write denial,
dependency change during `unblock`, symlink/oversize/invalid UTF-8, crash
during write, and disabled-lock environment. F01 resolves the architecture
question; it does not claim that this change is already deployed.

## Implementation (B00, KD-138)

The board runs **Backlog.md 1.53.0 plus one patch**, `common/board/backlog/digipaddock-board-transactions.patch`,
built by `common/board/backlog/build.sh` (upstream commit `fd20f71`, Bun 1.3.14 checked by SHA-256,
frozen lockfile, no dependency install scripts, the patch's tests run before the binary is produced) and
installed by `stage_setup.sh` as `/usr/local/bin/backlog` reporting `1.53.0-kd.1`. Every writer that goes
through Backlog — CLI, web, `kd-task`, agents, owner — therefore uses the same patched core:

1. **CAS inside the task lock.** `backlog task edit <ID> --if-revision <sha256> [--require-revision <ID>=<sha256>]…`
   locks the edited task and every guard task through `withTaskLocks` (sorted, the same lock directories as
   any other edit), re-reads them, compares the SHA-256 of each file's bytes and only then applies the edit
   through the normal update/serialization path. Mismatch → exit **3** (`Revision conflict`), lock held by
   another writer → exit **4**, nothing written. Success prints `Revision: <new sha256>` as the last line.
   Guards serve `unblock`: the dependency is locked and checked together with the blocked card. A
   revision-checked edit never opens the interactive wizard and cannot demote to Draft.
2. **Atomic task writes.** `saveTask` writes a hidden sibling temp file, fsyncs it, renames it over the task
   and fsyncs the directory; the old mode (and with it the ACL mask) is kept, the owner becomes the writer's
   uid and the default ACL of `tasks/` applies as for any new file. A symlinked or non-regular task file is
   refused. Unlocked readers (board sync, monitor, revision reads) see the old or the new file, never a
   prefix — including when the writer is killed mid-write.
3. **Revision reads are bounded.** Regular file only (`lstat` + `O_NOFOLLOW` + same inode), ≤ 1 MiB, strict
   UTF-8 (`readTaskRevision` in the patch, `common/kd/board_tx.read_revision` in Python — the same bytes give
   the same revision).
4. **Lock bypasses closed in the core.** `USE_GLOBAL_TASK_ID_LOCK=false` no longer disables the task, draft
   or create locks. Board moves (`reorderTask`, `moveTasksToStatus` — web drag and drop) were planned from an
   unlocked snapshot and wrote whole records, silently reverting a comment or assignment made in between;
   they now re-read each record under its lock and apply only the placement fields the move changed
   (status, ordinal, milestone). `task complete` moves the file under the task lock. The TUI editor edits a
   private copy and writes it back under the lock only if the file is unchanged; otherwise the user's text
   is kept and the edit fails. All of these were reproduced as failing tests against unpatched upstream.
5. **Python adapter.** `common/kd/board_tx.py` builds the finite argv (revision flags first, reserved flags
   refused in caller fields, at most 8 guards), runs it with a fixed environment and classifies the result
   as `ok` (with the new revision), `conflict`, `locked` or `error`; exit 0 without a revision line is an
   unknown outcome. `kd-task claim` uses it: revision first, then `task view`, then the CAS edit; a conflict
   makes it decide again (3 attempts). The separate `kd-claim.lock` is gone — it only serialized claims.

### Writer inventory after B00

| Writer | Path | In the transaction |
|---|---|---|
| Owner CLI (`kd-board run -- task …`) | patched CLI as `agent` | yes |
| Owner web (`digipaddock-board-web.service`) | patched core as `agent`; edits, moves, complete, archive, demote | yes (restart after install) |
| Owner TUI editor | private copy + locked compare-and-write | yes |
| `kd-task claim` | `board_tx` CAS | yes |
| sysadmin, dev, probe-agent instructions | `backlog task create/edit` | yes (plain edits are locked; no CAS) |
| Future BOSS (`kd-boss-board`, B02) | `board_tx` CAS only; no board ACL | yes, by construction |
| `kd-board sync/digest`, sysadmin monitor | read only | n/a — atomic files |
| Owner duplicate-ID repair (web/`doctor`) | rewrites colliding records under the create lock after its own preview-fingerprint check | create lock only; owner maintenance, never an agent path |

**Residual bypass (owner decision at the B gate).** The filesystem still lets uid `agent` (owner of
`backlog/`) and the agents' shared write group (`agent`, `agent-dev`) write task files directly, and root can always.
For the agents this is fenced by policy, not by the OS: the managed floor denies `Edit` on the board,
dev's sandbox lists the board in `denyWrite`, sysadmin's guard refuses writing commands. A direct
write cannot corrupt a reader (other writers still replace files atomically), but it is not covered by the
lock or CAS. Closing it at the OS level means a board-writer service with its own uid (the alternative
above) — not part of B00. BOSS itself has no write ACL (A05) and can only use the CAS path.

Stage gate before enabling any BOSS mutation: build and install on stage, then on an isolated board probe
two concurrent CLI writers, CLI vs web edit and web drag-and-drop, `kd-task claim` races, stale and guard
conflicts, a SIGKILLed writer, and file ownership/ACL after edits by `agent` and `agent-dev`.
