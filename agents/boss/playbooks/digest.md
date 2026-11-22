# Morning report (09:00 in the host's time zone)

On a `[digest] 09:00 …` wake-up, write one short message in the owner's language. Refer to cards by `KD-<n>` so the cabinet links them.

1. **Done yesterday** — one line for each card that became Done yesterday in the host's time zone. If any cards were cancelled or closed as duplicates, list them on a separate line under “Closed without completion.”
2. **Who's working on what** — the card each agent has In Progress, or say the agent is free.
3. **Blockers** — group open attention events and put escalations first.
4. **Waiting for you** — Review cards, Inbox cards with your recommendation (take, decline, or route to someone), cards blocked on the owner, and `@owner` cards with a proposal for how to handle them.

**Mondays — the weekly review of product data** (`spec:data-health`, the analyst's). Before the report:

1. Use the localized weekly title with the seven days before today, Monday to Sunday in the host's time zone. English format: `Product data: summary review for dd.mm–dd.mm`. Use the same title for repeats in the same week.
2. The project is the one of the newest card labelled `spec:data-health` (`kd-task list -l spec:data-health`). No such card yet: create none, and ask the owner in the report once which project the review goes to; his answer is that project from then on.
3. Check the `spec:data-health` cards in that project for an existing review covering the same dates, including a title in a previous owner language. If there is none, run `prepare-create --intent "<weekly product data summary review, in the owner’s language>" --project <project>`. Mark each previous week's candidate with `--reviewed` and give the different week as `--difference`. Then `create-inbox` with the weekly title. `ok KD-<n> create-inbox existing …` also means the card is already there. For an existing card, skip creation and proposal and mention it only if its proposal is still open.
4. Assign the new card with `assign --assignee @analyst --spec data-health --reason "<weekly review, in the owner’s language>"`, regardless of the project route. Use `propose-promote` with a localized reason naming the ⚠️/❌ data summary and period. Under Waiting for you, give the card, title, project and assignee in one line and ask whether to start.

The owner may ask for such a review on any day: a card of the same form, with his period in the title.

No evening report.

## Russian examples

- Report headings: «Сделано вчера», «Кто чем занят», «Заторы», «Ждёт вас».
- Weekly review title: «Данные продуктов: разбор сводки за дд.мм–дд.мм».
