# Selecting and reporting checks

Choose checks from the change's behavior, consumers and risk. A full run is not required after every edit or every PR.
The author records the selection and its limits; reviewers check that it covers the diff. The runner does not infer
dependencies or certify that a selected subset is sufficient.

## Policy

| Change | Required scope |
| --- | --- |
| Documentation only | Review links/examples and run `docs`; no runtime suite. Agent instructions, policy data and executable examples that change behavior are not documentation-only. |
| Frontend only | `frontend`, optionally selecting test files; add relevant browser scenarios for changed user flows. Include backend catalog/contract checks if shared inputs changed. |
| Isolated Python behavior | Explicit affected test modules and their consumers via `focused`; run `static` before committing. |
| Database schema/migrations, shared dependencies or test infrastructure; changes across permission, approval, identity, sandbox or executor boundaries | Full applicable regression on Linux, plus relevant integration, migration and refusal cases. |
| Unknown impact | Broaden to the full applicable suite; do not guess a narrow selection. |

Preserve meaningful refusal, stale-revision, concurrency and partial-effect cases. Avoid implementation-mirroring tests
and large matrices that add no distinct boundary. The full regression remains a release/risk check and a candidate for
a future scheduled runner; the GitHub CI below only advises.

During development run the selected tests. After a failure, fix it and rerun its module and affected consumers.
Repeat a broader run only when changed behavior, failures or unresolved impact justify it. A report-only change does
not invalidate unchanged runtime checks. Test-runner implementation changes need its own contract/failure tests and
static checks. Changes to shared pytest fixtures, discovery or the infrastructure existing suites execute under also
require a full run of those affected suites. Adding this explicit wrapper does not change their execution semantics.

Use one clean source snapshot and an appropriate environment. Do not duplicate an unchanged Linux run on Windows.
Windows checks cannot establish Linux permissions, flock, Unix sockets or systemd behavior. Record skip reasons.
Do not use production services or data for tests. Passing checks authorize no deployment or permission expansion.

## Explicit profiles

Use the prepared Python environment from README; this runner installs no dependencies.

```bash
# Small Python selection; an empty focused selection is an error, never the whole suite.
python scripts/check.py --profile focused --test tests/test_check.py
python scripts/check.py --profile static --base HEAD

# These profiles do not run the Python suite.
python scripts/check.py --profile docs --base HEAD
python scripts/check.py --profile frontend --web-test src/lib/stores/cards.test.ts

# Inspect a plan before execution; several profiles may share one report.
python scripts/check.py --profile static --profile focused --test tests/test_check.py --dry-run

# Full Python regression in an external prepared Linux environment. Includes the static gate.
python scripts/check.py --profile full --scratch-root /tmp --base HEAD

# Explicit, bounded Python parallelism; one file stays on one worker.
python scripts/check.py --profile full --workers 2 --scratch-root /tmp --base HEAD
python scripts/check.py --profile full --workers 4 --scratch-root /tmp --base HEAD
python scripts/check.py --profile focused --workers 2 --test common/tests/test_cabinet_flow.py --test common/tests/test_cabinet_server.py
```

`docs` checks Git diff whitespace, not links or correctness of prose. `static` runs `check_changed.py` over the diff
and untracked source files; it does not run tests. It also runs `check_history.py`: what a push would
publish — the commits not on `origin/main` (whatever `--base` says) and the uncommitted and untracked changes — read
as history: every commit message with its author and committer, every path and added line, by the public scanner
and, given a deployment's literal list, the closed one. The forge's history keeps such a line for good, even one a
later commit removed, so the check runs before the push; the required `devcheck/unit` runs the same
scan over the pull request's own commits, so such a pull request never reaches the owner. The list: `--private-list`, else
`KD_PRIVATE_SCAN_LIST`, else the running release's own; without one the closed scanner is skipped and the output says
so. A closed finding names the commit or path and line, never the literal. `focused` accepts explicit Python test files/node IDs, not directories
or pytest options. `frontend` runs type checks, selected Vitest files (all frontend tests if none selected) and the build.
`full` runs the static gate and the complete Python suite with slow-test durations; add `--profile frontend` when needed.
`ci` runs what GitHub CI runs (see [GitHub CI](#github-ci)).
Playwright remains an explicitly selected browser check using the existing prepared test servers; it is not implicit in
any profile. For another repository, use its own documented checks rather than this repository's profiles.

Commands run sequentially and stop on the first failure. No shell command string is executed. `--dry-run` writes no
artifacts and reports `planned`, never `passed`. Missing tools, interrupted execution and failing tests stay failures.

## Python workers

`--workers 1` (the default) runs ordinary sequential pytest. `--workers 2` or `4` explicitly enables the pinned
pytest-xdist with `--dist=loadfile`: each test file is executed by one worker, and different files can run concurrently.
This keeps file-local state together; it is not a lock for resources shared between files. No `-n auto` or global
parallel setting is added to pytest.ini. Small focused selections can be faster sequentially because each worker
imports and collects the suite. Parallel workers are rejected for profiles without Python tests.

For a large full regression in an external Linux environment with an allocated 4 CPU / 4 GiB budget, use 4 workers;
use 2 for a smaller budget. In one recorded comparison, pytest took 722.31 s sequentially, 386.25 s with 2
workers and 249.36 s with 4. The 2-worker run exposed a stale tester-text digest, corrected and rechecked with its
consumers (128 passed); the sequential and 4-worker runs passed all 9085 cases with 3 expected skips. These are
single measurements on a shared host, not a universal speed guarantee. Full commands including static and cleanup
took 732.893 s and 262.930 s for 1 and 4 workers respectively.

Use the prepared environment containing requirements-dev.txt. A missing xdist plugin is a test-command failure;
the runner never installs it or silently falls back. The root-managed developer environment checks that it imports.
Parallel failures preserve pytest's exit code; with `-x`, work already dispatched to other workers may still finish.

Each run gets a private scratch directory under its report directory by default, within the agent's writable checkout.
For an external prepared Linux environment, explicitly use `--scratch-root /tmp` to fit AF_UNIX socket names.
The parent must already exist; the runner creates its own private child and never cleans the parent. This option
grants no filesystem permissions. The live developer sandbox cannot write /tmp or bind local servers; do not expand
its permissions to run the full socket/integration suite. That suite requires an authorized external test environment
(the planned dev.check will provide one). Pure focused tests can run inside the sandbox with the default scratch.
At execution the runner adds `--basetemp <private-root>/<command-number>` to pytest and sets TMPDIR/TMP/TEMP for all
children. These generated paths appear in the executed argv in summary.json; `--dry-run` cannot predict them.
Pytest gives workers separate subdirectories. Scratch is removed after success or failure; full logs and reports
remain under tmp/checks. Do not point basetemp at an existing data directory when invoking pytest directly.

Current suites use per-test tmp_path databases, registries, fake host trees and Unix sockets; TCP servers bind port
zero. Installer/systemd commands are tested through substituted operations or shell harnesses, not the live host.
Session fixtures load helper modules in each worker. No current module requires a shared host mutation, so no
serial exclusion list is needed. Any future test that shares real services, fixed ports, users or filesystem state
across files must be isolated before inclusion in a parallel run; run such integration checks separately and
sequentially until then. Playwright's shared test cabinets remain sequential.

## Reports and further work

Verbose output is written to a unique directory under `tmp/checks/`; its path is printed at startup. Each command has
its full log, and subprocess temporary files use the run's private scratch. `summary.json` records profiles, commands,
exit codes, timings, platform, Git revision and dirty state.
Successful output is summarized; a failure prints at most 20 trailing lines plus the report path. Read more only when
diagnosing that failure. A green subset proves only that subset. Unexecuted commands after a failure have no result.

The runner does not cache successful verification, discover affected tests or change pytest's cache settings.
Reports are local evidence, not reusable CI attestations. Possible follow-up work covers persistent dependency
environments, source/environment-bound result reuse,
scheduled full regression and timing-led fixture improvements. Integrate those with the planned `dev.check` rather than
adding a second CI system. Keep full logs outside agent context and deliver a short completion result.

## Known vulnerabilities

The required `devcheck/unit` of this repository ends with the step `vulns` (`common/kd/vuln_scan.py check`, run by
the deployed tree in the check's child unit, with no network). It reads the lock files of the pull request's head
and of the target's commit the check is about (`refs/kd/base`) from git — every `package-lock.json`,
`composer.lock`, `yarn.lock`, `pnpm-lock.yaml`, `poetry.lock`, `Pipfile.lock`, `uv.lock`, and every
`requirements*.txt` pinned with `--hash=` (a file of version ranges such as `requirements-dev.txt` is no lock) —
plus the packages installed in the check's own venv, so the unpinned tools of `requirements-dev.txt` count by the
versions the check runs with. `osv-scanner` (version and `sha256` in `common/install/versions.json`) scans both
sides in one run against the offline OSV database (PyPI, npm, Packagist).

- **Red** — a pair «vulnerability + package» the base did not have; an id shared with any alias counts as the same
  vulnerability. A venv finding counts only for a package whose line the change touched in `requirements-dev.txt`,
  `common/cabinet/requirements-build.txt` or `common/cabinet/requirements.txt`. The step prints each new finding last
  (`NEW <id> <package> <version> (<ecosystem>) · fix <versions> · <file>`), so the check's notice in the card
  carries the id, the package, the version and the fixed one.
- **A line, not red** — a finding the base had already (`known …`): the daily scan below reports it.
- **No database, or one older than 72 h** — red, with the database's date and age.

Exceptions live in `osv-scanner.toml` at the repository's root, applied by the step itself (the scanner is given an
empty configuration):

```toml
[[IgnoredVulns]]
id = "GHSA-xxxx-xxxx-xxxx"
ignoreUntil = 2027-01-31   # at most 90 days ahead; effectiveUntil is accepted too
reason = "the parser is never given user input"
```

An entry without `reason` or a date, or with a date further than 90 days, makes the step red. Past its date the
finding blocks again, whether the base had it or not. Only `[[IgnoredVulns]]` is applied.

The daily service `digipaddock-vulnscan.timer` (04:20 in the host's time zone) runs `kd-vulnscan` as root, which
only orchestrates: the database is downloaded in a child unit as the `ci` user with network into
`/var/lib/digipaddock/devcheck/osv` (a failure is in the service's journal and fails the unit); root fetches the base
branch of every project of `/etc/digipaddock/repos.json` with the repo user's read key and hands its lock files to a
scan as the `ci` user with no network. A finding not seen before and not under a valid exception of that project's
`osv-scanner.toml` becomes one Inbox card of the project for `@dev`, whatever the project's route, through the cabinet's `card.create`; the seen
findings are kept in `/var/lib/digipaddock/vulnscan/state.json`, so a run again makes no second card, and at most 20
cards are made a run. While the database is older than 72 h, one card says it is not updated. `devcheck_setup.sh
install` (the factory component `devcheck`) puts the pinned binary in place, the directories and the units, turns the
timer on and starts the first download.

## GitHub CI

The public mirror runs `.github/workflows/ci.yml` on every push and pull request. It is advisory: the required check
of a change stays `devcheck/unit` in the forge, and a red GitHub run blocks nothing. It runs on `ubuntu-24.04` with
no secrets and read-only permissions. The workflow only installs packages; its checks are one command,
`scripts/check.py --profile ci`: the privacy scan, the static gate over the change, the Python suite with
`-m "not host"` and `KD_HOST_JSON=host/host.example.json`, an overlay made from `host/*.example.json` by
`kd.overlay init` (with stand-ins for the forge's host key and the closed scanner list) and checked, and the cabinet
web's type checks, tests and build. A test that needs the factory host itself (root, real users and homes, units,
sockets, files under `/etc/digipaddock`) carries the pytest marker `host`; it runs in `devcheck` and on the host, never
on the runner.

Repeat the CI run locally, on Linux, with the same command:

```bash
python -m pip install -r requirements-dev.txt
python -m pip install --require-hashes --only-binary=:all: -r common/cabinet/requirements-build.txt
python -m pip install --require-hashes --only-binary=:all: --no-binary=http-ece --no-build-isolation -r common/cabinet/requirements.txt
npm ci --prefix common/cabinet/web
python scripts/check.py --profile ci --base origin/main --workers 4 --scratch-root /tmp
```

`--base` is what the static gate compares with (the job passes the pull request's target, or the commit before a
push). Like `full`, the profile runs only on Linux; `--dry-run` prints its plan anywhere. The overlay is made in the
run's private scratch, so the run leaves only its logs under `tmp/checks`. A failure prints the failed command and the
tail of its log, in the CI job's output too. The profile installs nothing.

A tag `v...` runs `.github/workflows/release.yml`: an archive of the tree with the built web, `SHA256SUMS` and a keyless
Sigstore signature (`cosign sign-blob`), published as a draft release. The tag itself is signed with the owner's SSH
key; `kd-release` on the host verifies that signature against `host/allowed_signers` of the overlay. Every action is
pinned by its commit.
