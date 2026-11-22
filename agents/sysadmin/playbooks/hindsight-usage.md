# Reading `agent-hindsight-usage`

`ssh-ro {{host.stage_alias}} 'agent-hindsight-usage'` (no arguments) prints the memory server's own token usage — only numbers and model names, never transcript text:

- per unit (`hindsight-api`, `hindsight-worker`): its PrivateTmp start (`started=`), the count of `.jsonl` files, their size and the newest mtime;
- per day × model: the sums from `message.usage` per unit and in total (`total`), one call counted once, as `kd-usage` counts it;
- the `hindsight` rows of `usage.jsonl` for the last 14 days (`ledger`) with `coverage`.

Compare `total` with `ledger` to check `kd-usage`. `total` covers only what the running units still hold since their `started=`: for a day before a unit restart `total` is incomplete — that is not `ledger` being wrong.
