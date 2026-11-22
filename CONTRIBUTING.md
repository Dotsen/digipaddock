# Contributing to DigiPaddock

Original project code and documentation are licensed under the GNU Affero General Public License, version 3 only (`AGPL-3.0-only`); see [LICENSE](LICENSE) and [README.md](README.md#license). Submit contributions under that same license unless the maintainers explicitly agree otherwise. Preserve separate third-party license and copyright notices, and only contribute material you have the right to license.

## Propose a change

Use [GitHub Issues](https://github.com/Dotsen/digipaddock/issues) for bug reports and proposals. Before implementing a substantial change, open an issue with the problem, expected behavior, and acceptance criteria. Open a focused GitHub pull request for review and link the issue if there is one. Maintainers integrate accepted changes in Forgejo. Report suspected vulnerabilities privately using [SECURITY.md](SECURITY.md).

New agent types should select existing profiles and capabilities through their manifests. A new capability or an expansion of permissions needs an explicit design and tests. Keep deployment-specific hosts, domains, credentials, and topology out of shared code and examples; use clearly fictional data in fixtures.

Write code, identifiers, comments, docstrings, public documentation, and agent instructions in English. Owner-facing text is in the owner's language (`owner_language` of the deployment's `host/host.json`); the cabinet's own strings come from its ru and en catalogs. Label localized examples with their language and preserve exact command output and UI labels where needed. Follow the [engineering standards](docs/engineering-standards.md), including Python 3.12 for host code and Python 3.6 compatibility for target-server gates. Do not mix broad formatting or translation of legacy code into a behavior change. The root `.editorconfig` sets UTF-8, LF, and indentation defaults; preserve the local style of existing files.

## Verify the change

Use the environment and dependency installation commands in [README.md](README.md#local-development-and-tests).
Select checks under the [change/risk policy](docs/testing.md), rather than running the full suite after every edit or PR:

```bash
python scripts/check.py --profile focused --test tests/test_check.py
python scripts/check.py --profile static --base HEAD
```

To repeat GitHub CI before a pull request, run
`python scripts/check.py --profile ci --base origin/main --workers 4 --scratch-root /tmp` on Linux — the command the workflow runs. It needs the Python packages of README (`requirements-dev.txt` and the
cabinet's hashed locks) and the web's (`npm ci --prefix common/cabinet/web`); see
[GitHub CI](docs/testing.md#github-ci).

Replace the example test file with the modules covering the changed behavior and its consumers. Frontend-only work uses
`--profile frontend` with optional `--web-test` files. Documentation-only work needs review of links/examples and the
`docs` profile, without runtime tests. Behavior-changing instructions and policy data are not documentation-only.

Full applicable Linux regression is required for broad changes to schema/migrations, shared dependencies or test
infrastructure, permission/approval/identity/sandbox/executor boundaries, and unknown impact. Use `--profile full`
explicitly; the prepared external Linux environment can use `--workers 4 --scratch-root /tmp` with an allocated
4 CPU / 4 GiB budget, or 2 workers for a smaller budget, as described in
docs/testing.md. Add frontend or relevant browser/integration scenarios when affected. Windows cannot prove Linux behavior.
A failed command, skipped required scenario or unexecuted check cannot establish the required behavior. Record test skip
reasons and do not use production services or data for tests.

The static gate includes unstaged and untracked files and does not reformat them. Before committing, `HEAD` is the
baseline; after committing, pass the commit before the changes. Required missing tools fail; ShellCheck is optional.
Attach the short report and log path, check selection, environment and limitations to the PR. A green subset proves
only that subset; repeat checks when changed behavior or unresolved impact warrants it. See docs/testing.md for reuse
limits and the separate full-regression path.

Do not add tests that merely duplicate implementation details. Tests should establish behavior, including relevant failures, stale revisions, malformed input, and partial effects.

## Submit for review

Explain the concrete problem and resulting behavior in the pull request. Include the commands run, their results, skip reasons, and any checks still needed. Link the task and call out changes to capabilities, schemas, dependencies, or installation. Use small English Conventional Commit messages, such as `fix: reject stale card revisions`.

Never include credentials, private infrastructure data, authentication state, raw transcripts, or unredacted logs in commits, issues, or pull requests. Removing a value in a later commit does not remove it from history. Stop and use the private security-reporting path if you discover a potential exposure.

Merge and deployment follow the owner's approval process. Publishing a pull request, pushing code, or passing tests does not grant permission to merge or deploy. Installation scripts and changes to a running factory need separate authorization.
