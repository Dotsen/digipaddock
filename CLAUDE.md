# DigiPaddock

A factory for long-running AI agents of predefined types on one Linux host: an agent type is a profile plus capabilities from a finite registry; a new agent is an `agent.json` and instructions, with no code of its own. See [README.md](README.md) and [docs/overview.md](docs/overview.md).

**Deployment-specific values never enter this repository** — of the first deployment or any other: no host names, addresses, domains, server inventory, forge accounts or bot names. A deployment keeps them in its own private overlay repository, laid over this tree at release time; the contract is [docs/deployment-overlay.md](docs/deployment-overlay.md). Code reads them through `kd.naming.load_host()`, `@host:<key>@` tokens (units, nginx, registries) and `{{host.<key>}}` (agent texts). Tests use the synthetic values of `common/tests/fixtures/host/host.json` (`example-stage`, `203.0.113.10`, `*.example.test`).

## Layout

| Path | What |
|---|---|
| `common/` | The shared platform: installers (`common/install/`), systemd units, the board and coordination tools, capability channels, the memory client, the Python package `common/kd/` |
| `common/kd/cabinet/`, `common/cabinet/` | The board in SQLite behind one service and the owner's web cabinet (FastAPI; Svelte 5 PWA in `common/cabinet/web`) |
| `common/agent-capabilities/`, `common/agent-profiles/`, `common/agent-sandboxes/`, `common/agent-resources.json` | The finite registries an agent manifest selects from |
| `common/agent-template/` | Instructions shared by every agent |
| `common/coordination/` | Routing, stall-detector rules, delegation limits, the factory's operation catalog |
| `common/specializations/` | Versioned specialization texts per agent type |
| `common/worker/` | Worker slot registry |
| `agents/<name>/` | One agent type: `agent.json`, `CLAUDE.md`, playbooks, and code only for modules the registry does not have yet |
| `host/*.example.json`, `agents/sysadmin/*.example.*` | Example overlay files: the shape a deployment fills in |
| `docs/` | Public documentation; `docs/adr/` — architecture decisions |
| `scripts/` | Development checks and probes |
| `tmp/` | Scratch files (ignored) |

## Rules

- Code, comments and agent instructions are in English. Text the owner reads (cabinet, notices) is in the deployment's owner language.
- Standards: [docs/engineering-standards.md](docs/engineering-standards.md). Contributions: [CONTRIBUTING.md](CONTRIBUTING.md). Security: [SECURITY.md](SECURITY.md).
- Code installed on managed servers (the read-only gateway) must run on Python 3.6.
- A behaviour change of an agent or the platform updates the public docs in the same change.
- Nothing here is deployed by a push. A release is `kd-release` on the deployment's host: `main` of this repository and of the overlay, both scanned, composed and switched.

## Checks

Select checks by the change/risk policy in [docs/testing.md](docs/testing.md):

```bash
python scripts/check.py --profile focused --test <test file>
python scripts/check.py --profile static --base HEAD
pytest -q                                   # the full Python suite (Linux for everything)
npm --prefix common/cabinet/web test        # the cabinet's frontend
python3 -I common/kd/privacy_scan.py . --tracked   # the public privacy scanner, exit 0
```

Run the privacy scanner before every commit that adds text: it fails on public IPv4 addresses, forbidden domains (`privacy-scan.json`) and secret-like strings.
