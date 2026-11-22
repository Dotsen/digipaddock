# Provider keys from the cabinet

The cabinet installer installs `digipaddock-model-keys.socket` and its root service, the
`kd-model-keys` command and the root-owned signing registry `/etc/digipaddock/model-key-signers` (0700).
It creates `/etc/digipaddock/llm` (root:root 0711); each key file remains root:root 0600. No key is
returned by the API. A fresh installation has no enrolled signers and refuses changes until root
explicitly enrolls one. Installing or restarting never enrolls cabinet keys automatically.

As root on the factory host, after the owner has added a signing passkey in the cabinet:

```bash
kd-model-keys enroll <owner-id>                     # list current signing credential IDs, without public keys
kd-model-keys enroll <owner-id> <credential-prefix> # choose one ID, at least eight characters
kd-model-keys revoke <owner-id>                     # revoke all enrolled keys for this owner immediately
```

Enrollment accepts only an active user with the global owner role. Repeat it for another chosen
passkey if needed. Revocation is read on every operation, including an already issued challenge.
Removing a signing key in the cabinet does not change this registry by itself: root's registry is independent of the
cabinet database. `kd-signers status` shows where it differs from the cabinet, and `kd-signers sync --apply` brings it
(and kd-review's) to the cabinet's set of signing keys — a removed or switched-off key leaves, a new one joins; the
servers' exec gates take a changed set only through `deploy_target.sh <alias> --replace-signers` from the owner's
machine (see [cabinet-cli.md](cabinet-cli.md)).

The owner opens **Settings → Models**, the **Provider keys** tab (a model without its key links there), enters the bare token in the provider's **Key** field and
chooses **Sign and save key**. Deletion uses **Delete key**, a confirmation and another passkey
signature. Each root-generated challenge lasts five minutes, is bound to the owner, provider, action
and key digest, and is consumed once. A second challenge replaces the owner's first outstanding one.
The token passes to root only on the signed save, over a Unix socket restricted to the cabinet UID
(filesystem permissions and `SO_PEERCRED`). Root accepts only catalog provider paths. There is no
network access, arbitrary secret name or read-key operation.

After saving, `kd-secret status` must report `llm-<provider> ok`; `stat` of its catalog file must report
root:root 0600. The feed shows the change, author and time. No token, digest or assertion is stored in
the board or logs. If the feed is unavailable after the filesystem effect, the API explicitly reports
`audit: false` and the UI says the key changed but its event was not recorded; root's journal retains
the actor, provider and action without the value. Check the file before retrying after a timeout.

```bash
systemctl status digipaddock-model-keys.socket digipaddock-model-keys.service
journalctl -u digipaddock-model-keys.service
kd-secret status
```

Stopping and disabling `digipaddock-model-keys.socket`, then stopping its service, disables cabinet key
changes. Existing key files and root's `kd-secret put llm-<provider>` remain available. Removing a
provider's file makes new sessions and worker jobs on its models fail `no_key`; running processes
retain their existing environment until they end. Restarting the key service invalidates outstanding
challenges, so the owner starts a fresh signature; a previously used signature cannot be replayed.

Browser checks use a rootless adapter confined to the dev cabinet's state directory and explicitly
enrolled fixture public keys. They do not touch host keys or services. Linux tests separately exercise
peer UID checks and the common writer; its root-only disposable probe verifies actual root:root 0600.

## Subscription and direct API credentials

The keys tab distinguishes the Claude subscription token, the direct Anthropic API key, the direct OpenAI API key
and the existing provider API keys. The subscription remains the default when no cabinet model is selected. It is
not a model row and cannot be chosen as a provider of a new model. The subscription's own models (`claude:<alias>`,
listed first in Settings → Models) have no key of their own: a session on one runs on this token.

- **Claude subscription:** signed replacement of the fixed `claude-token` secret in `/etc/digipaddock/claude.env`,
  `CLAUDE_CODE_OAUTH_TOKEN=<token>`. Deletion is refused by the root protocol as well as hidden in the UI. The
  existing `claude_token` validator checks the format. After writing, the same `kd-secret` path calls
  `systemctl try-restart hindsight-api.service hindsight-worker.service`: running Hindsight processes reload it;
  inactive services remain inactive. New agent sessions and worker jobs read the new file normally. An unsuccessful
  restart is reported separately from a successful write, so the owner is not told that the old token remains.
- **Anthropic API:** `llm-anthropic`, `/etc/digipaddock/llm/anthropic.env`, `ANTHROPIC_API_KEY=<key>`. Models of the
  `anthropic` provider go directly to Anthropic using API billing. This credential is separate from the subscription.
- **OpenAI API:** `llm-openai`, `/etc/digipaddock/llm/openai.env`, `ANTHROPIC_AUTH_TOKEN=<key>`. The variable names the
  Claude Code gateway credential; the value is the owner's OpenAI API key. Models of the `openai` provider use
  OpenAI Responses through `digipaddock-openai-bridge.service`. The key never goes into command arguments.

All files remain root:root 0600. The key writer needs the parent `/etc/digipaddock` writable inside its service
sandbox for atomic replacement of `claude.env`; the signer registry is explicitly mounted read only there. The
protocol still accepts only the catalog providers and the single fixed subscription secret, never a path supplied
by a browser. The same root-enrolled signing passkey works for each type.

The subscription block shows age, estimated expiry and warnings at 30 days, seven days and the estimated end.
Expiry is **365 days from the file's mtime**, not a token introspection result or an assurance about account access.
The existing host token-age notifications remain in effect. The block's **Probe** queues a one-turn, no-tools
subscription run through the session host. It reports failure for a missing or malformed file and uses no provider
API key or stored user login as a fallback. It does not restart existing agent sessions.

The OpenAI adapter listens on loopback only and authenticates every request with the current provider key before
forwarding anything. The upstream is fixed to `https://api.openai.com/v1/responses` (and its `input_tokens` endpoint);
redirects are refused. It supports text, images, PDF input, client function calls, tool results, cached-token usage
and opaque encrypted reasoning across tool rounds. Claude Code retains execution of tools under the existing guards,
capabilities and approvals; the adapter executes no tools. MCP tools load upfront (`ENABLE_TOOL_SEARCH=false`).
Claude-only server tools are refused. Responses are buffered, then returned using Anthropic JSON or SSE framing;
this does not stream individual OpenAI tokens as they are generated. API-key rotation invalidates the old adapter
credential for subsequent requests; start a fresh session for an already running process using the previous key.
The model must support Responses and function calling. The current account, model availability and billing are
verified only with an actual key and an explicit probe; tests use a fake upstream.

The API contract follows the official [Responses function calling](https://developers.openai.com/api/docs/guides/function-calling),
[reasoning state](https://developers.openai.com/api/docs/guides/reasoning) and
[token counting](https://developers.openai.com/api/docs/guides/token-counting) documentation.

The native Anthropic API key is scrubbed from every sandboxed Bash command as well as the subscription and provider
credentials. This raises the generated policy schema to **7**; reinstall generated agents after release. The release
process's normal policy refresh applies the new schema. Do not bypass a launch refusal with an old policy.
