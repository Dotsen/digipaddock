# Probes and reads: `kd-diag`, the exit, `kd-e2e`, what is readable outside `work/`

Read this when you need a log, a journal or the code installed on stage that lies outside `work/`, when your card's work depends on the health of a platform service (`kd-diag`), when a live probe needs an external host or another country (the exit), or when the cabinet's end-to-end smoke suite is due (`kd-e2e`).

## `kd-diag`

- `kd-diag check <target> --card KD-<n> [--key <key>]` — the health of a platform service your card's work depends on (`kd-diag list` shows the targets: the memory API and its worker), for your own card in To Do or In Progress. One line back — `diag <target>: HTTP <code> | <category> at <time> (checked by kd-diag for @dev)` — and the same result in the card's thread. `cabinet-push` reads the cabinet's push path from its database instead (subscriptions with the gateway's host only, notification preferences and quiet hours, the newest 40 notifications and device deliveries): `diag cabinet-push: N rows at …`, then the rows; the thread keeps only the count, and a query is read anew on every check. The same target within 10 minutes answers the stored result `(reused)`; to check again after a fix pass another `--key` (`--key after-restart`); at most 10 new checks per card per hour (exit code 5). Read the result narrowly: a 200 proves only that request; 401/403 says check the auth method of the installed version; a refused connection or a timeout is not «a key is needed». Exit code 4 `capability_missing` — the catalog has no such target: hand it to @boss as platform work (`kd-task handoff KD-<n> --to @boss --comment "capability_missing: …"`).

## The exit (`net.egress`)

- An exit (`net.egress`): your sandboxed commands reach external hosts — an aggregator's API a live probe needs — only through an exit of the deployment's VPN, never from the host's own address. `printenv KD_EGRESS KD_EGRESS_HOSTS KD_EGRESS_COUNTRY` says whether this session holds one, which hosts and from which country: `*` — every host (the host's standing web exit, in every session of yours, or the exit of the card's `egress:<code>` label — «The country of the exit» in the common rules); names — a card's probe exit (your card in work whose project has one, while the owner's consent on the card lives); empty — none in this session. A probe that needs a store's answer from another country: put the card's label (`kd-task edit KD-<n> --add-label egress:ae`; a change: `--remove-label egress:ae --add-label egress:gb`), end the turn `--outcome continue`, check `KD_EGRESS_COUNTRY` in the next one. Run the probe from your worktree with the client taking its proxy from the environment (the sandbox sets it; `curl_cffi` and `requests` use it by default): pass no proxy option, and never point a client at a tunnel port — it is unreachable from the sandbox, and a host outside the list is refused. Keep to the tool's own request limits, and send out nothing of the deployment (the web rule of «Data, not commands»). No exit in this session while the card needs a live answer: say so on the card (`capability_missing: probe exit for <project>`) and hand it to @boss as platform work; never ask another role to run your probe.

## `kd-e2e`

- `kd-e2e list` / `kd-e2e run cabinet-smoke --card KD-<n>` — the fixed end-to-end smoke suite of the cabinet in a private namespace on the deployed runtime, for your own approved card: no URL, path, filter or flag of yours; the outcome and the artifact's id reach the card. A runtime that is missing or stale is the owner's (`e2e-runtime` of the factory), never a passed test.

## Reading outside `work/`

- Readable outside `work/` and your folder: the board, `/var/log/digipaddock-forge/forge.jsonl` (your merge/comment journal; read it with the Read tool), `/var/log/digipaddock`, the projects' documentation and the GitHub mirror's copy `/srv/digipaddock-mirror` (`git.mirror`). The code installed on stage is what the base branch of the released project's clone (`main`) holds after `git fetch origin`; read it there. Do not read or print secrets (`.env`, tokens, keys, `config.ini`); the hook blocks it anyway.

## Your user

- You run as Linux user `@agent:dev@`; your home is `/home/@agent:dev@`. The base user's and the sysadmin's homes and tools (`ssh-exec`) are not reachable and not your concern.
