# ADR 0004: The board is the cabinet's own SQLite store, not Backlog.md

- Date: 2026-10-15
- Status: accepted; implemented in KD-225 (board core of the owner cabinet). Later (2026-11-07): the one-time
  `kd-cabinet import` below has been removed; the nightly `kd-cabinet export` stays
- Supersedes: [ADR 0001](0001-boss-board-transactions.md) (BOSS writes joining Backlog.md's task transaction)
- Related: the owner cabinet's design (internal spec of the first deployment), decision 4; KD-224…KD-231

## Context

The board was Backlog.md: one Markdown file per card, a pinned fork (`1.53.0-kd.1`) only to get a compare-and-set
revision for concurrent writers (ADR 0001), a Bun build, a web process on a local port behind nginx, and every rule of
who may write which field (`kd-task`, `kd-boss-board`, the watch detector) implemented around files the fork rewrites.
Two stores had to agree: the md files and our own journals (activity, attention state, grants). Times, authors and
revisions were inferred from file contents and mtimes (ADR 0003 records how weak that evidence was).

## Decision

The board is one SQLite database, `cabinet.db` (WAL), owned by a service user (`<prefix>-cabinet`) and opened by exactly
one process, `kd-cabinet serve`. Callers reach it through a unix socket; the caller is identified by `SO_PEERCRED`
(uid → principal: the base user's executor, an agent, the exec or approve service, root), and only typed operations
exist. Every write is one transaction of rows plus an append-only event, with an integer `revision` per card for
compare-and-set; the board rules (transition table, field rights, `Inbox` boundary) moved into the service unchanged in
meaning. `kd-task`, `kd-boss-board`, `kd-board sync`, the watch detector and the worker runner are clients of the socket.

Markdown stays only at the edges: a one-time `kd-cabinet import` of the existing `backlog/tasks/*.md` (dry run with a
report first; refusals block `--apply`), and a nightly `kd-cabinet export` of the whole board into Backlog.md-shaped
files in the board's git (backup and grep; nothing reads it back). The fork, its build, `board_tx` and the web process
are removed.

## Consequences

- One writer: no file locks, no fork, no revision derived from a file hash; conflicts are refusals with the current
  revision, as `--if-revision` was.
- Access is a group (`digipaddock-cabinet`) on the socket plus the principal check inside the service, not filesystem ACLs on
  card files. Running processes see a new group only after a restart.
- Until the cabinet's web pages arrive the owner has no web board; `kd-cabinet cards` and the nightly snapshot
  are the read paths.
- Backlog.md-specific behaviour documented in ADR 0001 and ADR 0003 no longer applies; the activity contract of ADR 0003
  (trusted producer, no inference from `updated_date`) still holds and is now served by the cabinet's events.
