# Checks of a pull request: required checks, the history step, locked dependencies

Read this when the project's passport names required checks and you ask the factory to run them (`kd-forge check`), when its outcome wakes you, when the static check's history step flags a line, or when the tests need the project's locked dependencies (`kd-deps install`). «Workflow» and step numbers refer to the Workflow section of your main instruction.

## The history step of the static check

- Its history step reads every commit not on `origin/main` and the uncommitted changes as history (messages, authors, paths, every added line; the release's closed literal list): a line flagged there, even one a later commit removes, stays in the forge's history for good once merged. A finding — move the work onto a fresh branch from `origin/<target>` (`git merge --squash` of the old one, one new commit). A PR that already carries such a commit stays red on `devcheck/unit`, whose `history` step reads the same: open the new PR with `kd-pr` from the fresh branch and tell the owner the old one is to be closed.

## Waiting for a contour

`kd-work finish KD-<n> --outcome wait_dependency --contour <name>` waits for a check contour to leave maintenance.

## Reading the result of a check

- Read the short result and log path, then only the failure details you need; after a fix rerun the failed module and its consumers. A green subset proves only that subset; Windows does not prove Linux.

## `kd-forge check`

- `kd-forge check <project> <N> [--profiles a,b]` — asks the factory for a `dev.check` of the PR's live head (by default the passport's required profiles): a clean checkout of the head with the other components of the passport's contour, the tests under their own user with no network and no sockets, the `devcheck/<profile>` statuses on the head. It prints `проверка <id12> в очереди контура …` and the exact `kd-work finish KD-<n> --outcome wait_dependency --operation <id> --note "…"` — end the turn exactly so; never wait in the turn. The same head and profiles answer the same check (`уже идёт` — the same wait; `уже пройдена` — nothing to repeat). It refuses before the factory when the check would mean nothing: the PR not open, no `KD-<n>` in its title, the target outside the passport, no contour in the passport, or `base_stale: база <target> ушла вперёд …` — merge the target first (`base_moved` in playbooks/merge-and-release.md). The check's end wakes you: read the `checks:` line of `kd-task view KD-<n>` — `succeeded` → `kd-forge publish`; `failed … — checks_failed` → `kd-host-read tail -n 200 devcheck/<id>.log` (the full log: `kd-host-read cat devcheck/<id>.log`), fix, commit, `kd-pr`, check again; `environment_not_ready` (or `kd-forge check` refused with `environment_not_ready: the contour <name> is in maintenance …`) — not your code: one line to the owner and `kd-work finish KD-<n> --outcome wait_dependency --contour <name> --note "…"`, the contour's release wakes you; `dependency_update_required`, `external_unavailable` — not your code either: the notice in the thread says what the owner or the platform must do; one line to the owner and `--outcome wait_user`; `superseded` — a newer head or the card moved: check the current head; `cancelled`, `host_lost` — the check was stopped or the host lost it: check the current head again.

## `kd-deps install`

- `kd-deps install` — the locked dependencies of the project whose working copy you stand in (`work/<Repo>` or the card's worktree, at its top): `composer.lock` → `composer install` and `package-lock.json` → `npm ci`, with no install scripts and no composer plugins, through your exit, the caches under `work/.cache`. The dependency folder (`vendor/`, `node_modules/`) must be ignored by git — it never goes into a commit. It is the only form: `composer`, `npm`, `npx` and `pip` directly stay refused, and it adds, updates or removes nothing — a new or newer package is a change of the lock file in your pull request, agreed with the owner first (Workflow 4).
