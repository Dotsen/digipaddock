# Before you create a card

A request for new work (“create a card…” (Russian: «заведи…»), “we need a card for…” (Russian: «нужна задача на…»)) is first a question what is needed, then a look at the board, and only then — maybe — a new card.

1. **Understand the need.** What result does the person want, in which project? A repeated request may be a complaint that something already done is broken.
2. **Look.** `kd-boss-board prepare-create --intent "<the need in one line, in the person's words>" --project <project>`. It prints a token and the candidates — cards of any status that share words with the intent (`<id> <status> · <project> · revision <n> · score <n> · <title>`). One look may miss a synonym: look again with other words (`голосовые`, `аудио`, `распознавание`, `voice`) — every look gives its own token; use the one whose candidates you read. `kd-task search "<words>"` finds more (`a|b` — either word; any case, ё as е).
3. **Read the candidates.** `kd-task view` and `kd-task scope` of each one that could be it: its goal, scope, acceptance criteria and state. A title alone is not enough.
4. **Decide:**
   - **The same result is already planned** (an open card): no new card. Add the wish to that card and answer with its number. The analyst holds it (`@analyst`, `spec:task-brief`): `request-brief --card KD-<n> --revision <rev> --reason "<the wish>"` — an addition to the brief it writes. Any other card: `comment` it with the wish (a `request-brief` there would send the card back to the analyst and start its brief again — do that only when the wish changes what the brief settled, and say so to the owner).
   - **A part matches**: create only the missing part, and name the boundary in `--difference`; link the cards with a comment, not a dependency, and never call it a duplicate.
   - **The function is Done and the person says it is broken**: a regression, not a new feature — create a localized “Regression: …” naming the Done card, or add to an open regression card of it.
   - **Not enough to tell**: one concrete question to the person, with your recommendation. Never create a probable duplicate just in case.
5. **Create** only the missing card: `kd-boss-board create-inbox --title "<title>" --project <project> --reason "<why>" --token <token> --reviewed KD-<a> --reviewed KD-<b> --difference "<how it differs from each>"`. Every candidate of that token is `--reviewed`; `--difference` is required when there were candidates. The cabinet checks the look again: a changed candidate (exit 3; a moved status counts, a note does not) — look again and decide again; the same intent created meanwhile, or an open card of the same title, returns that card (`ok KD-<n> create-inbox existing …`) — tell the person its number, not “created”. The token lives 30 minutes.
6. **Answer** in one or two lines with the result: Russian example: «Голосовые уже есть в KD-9032 — дополнил его выбором движка. Изображения и файлы — новая KD-9041, передал аналитику.» Not «создал две карточки» when one need was already there.

The token proves you looked, not that you judged right: the meaning of «the same» is yours to judge, carefully.
