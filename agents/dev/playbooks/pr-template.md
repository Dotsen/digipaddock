# PR body template

Write the PR body in the owner's language (`owner_language`). Keep the five section headings exactly as shown. Replace the prompts below with the actual results.

### Russian examples

Use these only for a Russian-speaking owner. Omit this examples section from the PR.

| Section | Example |
| --- | --- |
| Checks | `нет` |
| Review | `совет кабинета: см. карточку` or `ревью не проводилось: у карточки нет совета по коду` |
| Rollout | `нет`, or headings `До мержа`, `После деплоя`, `Проверки`, `Откат` in that order |
| Cage | `да, файлы: …` or `нет` |

KD-<n> — {{host.cabinet_origin}}/cards/KD-<n>

<2–6 lines: what was done and why; link the card's acceptance criteria you covered>

## Checks
<What you ran and the results, such as `pytest: N passed, M skipped`, check profiles, manual checks or probes. Say if you ran none. This section informs the owner. Required checks are the factory's `devcheck/<profile>` statuses on the head (`kd-forge check`). `kd-pr` appends `Checks (passport): …`.>

## Review
<Point to the council record on the card. The card's council of models reviews the published head (CLAUDE.md, Workflow 5). On a later head, add one line on what you fixed after its findings. If the card has no code council, say that no review was run and give the reason.>

## Risks
<What could break and how to roll back, or `-`.>

## Rollout
<Say “No” in the owner's language if the merged and deployed code needs no additional server steps. Otherwise, give one block per environment from `apps` in the sysadmin's operation catalog (`agents/sysadmin/operations.json` in the deployment overlay). Keep the following order in each block.>

1. **Before merge** — list the steps or say there are none.
2. **After deploy** — number the steps and explain why each is needed. Use only `app.tool <tool> <arguments>`, `app.flag [<section>] <name>_enabled = 0|1`, or `service.reload <unit>` from the catalog.
3. **Verification** — name the expected HTTP status, log line, or tool output.
4. **Rollback** — describe how to restore the previous state.

<Keep shell, SQL and commands for the owner out of the sheet. Steps the catalog cannot express belong in the project's deployment or tools (`playbooks/merge-and-release.md`). After merge and deploy, hand the sheet to the sysadmin yourself. The owner signs the sysadmin plan where required.>

## Cage
<List the changed cage files or say “No” in the owner's language. The cage covers every changed file except `docs/**`, `tasks/**`, `**/tests/**`, and plain `*.md` outside `agents/*/CLAUDE.md` and `agents/*/playbooks/**`. `kd-pr` computes the list from the diff against the live target branch, appends `Cage (computed): …`, and rejects a body that says `No` or `нет` when the computed list has entries.>
