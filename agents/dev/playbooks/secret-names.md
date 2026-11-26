# Secret paths and accounts the hook refuses

Read this when the hook refuses a command, a board note, a commit message or a PR body for naming a secret, or before you write a text that may name one.

- The hook refuses a Bash command whose text names a secret path or account (`.credentials.json`, `claude.env`, `pult.env`, `/etc/digipaddock/llm`, `coding-agent.json`, `coding-agent.env`, `/etc/hindsight`, `.hindsight/`, `/etc/digipaddock/forge`, `forge/`, `{{host.forge_merge_user}}.env`, `{{host.forge_dev_user}}.env`, `{{host.forge_review_user}}.env`, `{{host.forge_import_user}}.env`, `@forge@`, `@exec@`, `.ssh/`, `_ed25519`, `.git-credentials`, `config.ini`, `.env`) — inside a board note, a commit message or a PR body too: reword it.
