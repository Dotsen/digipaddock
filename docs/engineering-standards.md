# Engineering standards

These rules apply to new work in digipaddock. They are intentionally incremental: existing code is not reformatted merely because a neighboring line changes.

## Language and boundaries

- Write new or substantially edited code comments, docstrings, identifiers, and test names in English. Explain non-obvious *why* and security invariants; do not narrate the next line. Translate legacy comments in separate, behavior-preserving changes. Owner-facing cabinet, notice, and audit messages are in the owner's language (`owner_language` of the deployment's `host/host.json`, the ru and en catalogs); do not silently change their interface language.
- Keep policy data and agent-specific instructions in `agents/<name>/` and the finite capability registries. Put shared decisions in `common/kd/`, thin CLI adapters in `common/bin/`, and installation orchestration in `common/install/`. A new trust boundary or external effect needs an explicit capability and tests, not an arbitrary JSON field or a more permissive prompt.
- Prefer a functional core with explicit inputs and results. Keep filesystem, process, clock, network, and messaging effects at narrow boundaries so tests can supply substitutes. Use type hints for new public Python APIs. Avoid speculative frameworks, duplicated validators, `shell=True`, `eval`, hidden subprocesses in pure functions, and catch-all error handlers that turn security failures into success.
- Validate untrusted data before effects; fail closed on unknown fields, paths, statuses, and command forms. Preserve existing owner approval and OS-level permission boundaries. Tests cover both allowed behavior and refusals, including malformed input, symlinks, stale revisions, concurrency, retries, and partial failure where relevant.

## Compatibility and verification

- Host-side code targets the agent host's runtime (Python 3.12, Ubuntu 24.04). Code deployed to target-server gates must remain Python 3.6-compatible and pass the existing `vermin` tests. New shell scripts use `set -euo pipefail`, quote arguments, and construct finite argv arrays rather than shell strings.
- Keep dependencies small. A new dependency needs a documented purpose, license, supported Python version, and reproducible install path. Never put tokens, private keys, Claude authentication, or raw transcripts in code, fixtures, logs, PRs, or Git history.
- Use small English Conventional Commit messages. Review the exact diff and select checks under [docs/testing.md](testing.md): affected behavior and consumers for ordinary edits, full applicable Linux regression for broad risk or unknown impact. Do not require the full suite after every edit or PR. For changed code, run `python scripts/check_changed.py --base <base-commit>` before committing. It checks Git whitespace, Ruff lint on new/changed Python, Ruff formatting on **new** Python, and `bash -n` on changed Bash. Existing Ruff findings are grandfathered only in unchanged legacy lines; new findings fail. If ShellCheck is installed, new Bash files are checked with it too. Legacy Python files are not reformatted wholesale by this gate.
- `check_changed.py` defaults to `--base HEAD` for an uncommitted change. After committing a branch, pass the branch-point SHA or ref explicitly. It does not replace tests, code review, live Linux sandbox/ACL checks, or the owner approval required for deployment. If a required checker is unavailable, the gate fails with an installation hint; optional ShellCheck reports a skip.

## Working files

- Work is tracked on the board, one card per piece of work; a deployment's own plans, protocols and status live in its overlay, not in this repository. Outcomes go to the card and its result.

## GitHub publication gate

Before any GitHub push, audit tracked files and **all reachable Git history** for secrets and operational details; rotate exposed credentials and obtain owner approval before any history rewrite. Decide with the owner whether the destination is private or public and which license, if any, applies. Public release additionally needs an infrastructure-disclosure review, English README, contribution and security-reporting guidance, CI with minimal token permissions and no production credentials, and protected review for security-critical paths. Ignore rules do not remove secrets already committed.
