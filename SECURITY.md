# Security policy

DigiPaddock runs agents with access to code and operational capabilities. Its security boundaries include Linux identities, the sandbox, capability executors, revision checks, and recorded owner approvals. Instructions followed by a model are not a substitute for those boundaries. See the [security model](docs/security-model.md) for the design.

## Report a vulnerability privately

Do not report vulnerability details in a public issue, pull request, discussion, or commit. Do not upload credentials, private keys, authentication state, infrastructure inventories, raw agent transcripts, database copies, or unredacted logs.

On GitHub, use **Security → Report a vulnerability** when the form is available. You can also reach the maintainer through his [public contact page](https://dotsenko.pro/) to arrange a private reporting channel. In that first message, give only the affected component and commit.

Start with the affected component and commit, the expected boundary, the observed behavior, and a minimal reproduction with synthetic data. Describe the potential impact and any workaround. Send only the information needed to reproduce the problem. If a report involves a credential, describe its type and location without sending its value. Coordinate sensitive evidence and disclosure privately with the owner.

If your own credential was exposed, revoke or rotate it through its issuer. Deleting a file or editing a Git commit does not revoke a credential.

## Testing boundaries

Use an isolated environment that you control, with synthetic accounts and data. Do not probe a running deployment, managed server, third-party service, or another user's account without separate explicit authorization. Access to source code is not authorization to test those systems. Avoid tests that transmit data to an external endpoint or alter real approval records.

## Support and disclosure

There is no published supported-release series or guaranteed response time yet. Include the exact commit in a report; maintainers will determine affected versions and a fix. A local test result does not establish that a deployed version is safe.
