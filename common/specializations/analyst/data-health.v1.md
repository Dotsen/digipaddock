# Specialization: data-health (v1)

## Purpose

Review the warning and failure verdicts of a product's daily data-health summary for one period and report on the card what dropped and why, so that a silent breakage of data collection is noticed within a week, not after months. Read and report; nothing is repaired here.

## Inputs

The card: the period (by default the 7 days before today in the host's time zone, ending yesterday; a title «… за дд.мм–дд.мм» names it) and an optional filter by product, aggregator (marketplace) or country. The product's repository copy (`/srv/digipaddock-repos/<project>`, history through `kd-repo`): the job that builds the summary, the code that turns a day's numbers into a verdict, the migration of the daily table it reads, and the product's documentation of the rule. The production database of that product through `kd-data`. Record the commit of the verdict code you read.

## Procedure

- Find the sources in the product's repository, not in memory: the summary job, the verdict function with its thresholds and norm (how many previous days, which days count as live, when a slice is new), the daily table and its columns, the documented rule. Never copy thresholds from an earlier card: the code may have changed.
- Pick the database: only a production database of the product among those `kd-data` lists (`kd-data x "SELECT 1"` refuses and names them); a staging, test or demo copy is out of scope. Not readable — stop with `missing_inputs`.
- Take one snapshot of the daily table for the period and for the days the norm needs before it: name the columns, filter by the indexed day column, `LIMIT` the rows, `EXPLAIN` a heavy query first. One statement per call; a refused or cut answer is written as such, never guessed around.
- Recompute every slice's verdict per day with the thresholds of the code you read. What the table cannot show — planned targets, a changed set of restaurants, a pause kept elsewhere, a correction the job applies outside the table — mark on the slice as not recomputable, not as healthy.
- For each warning or failure slice look for the cause: the product's raw tables of that slice (filtered by an indexed column, limited, aggregated — counts and dates, never rows of customers); commits of the product's repositories within 3 days before the drop (`kd-repo log <project> --since … -- <path>`, `kd-repo show`); the product's documentation and the board's cards of that product. Classify it: `collection` (data stopped arriving), `parsing` (arrives, but is read wrong), `configuration` (switched off, paused, a setting changed), `real_change` (the restaurant or the marketplace really changed), `unknown` (not established — say so; a guess is not a cause).
- A cause needs evidence: the query and its result, the commit, or the document and its place. A slice whose cause you could not establish goes to `unknowns` with what you checked.
- Write the result as the card's report and nothing else. Proposing a fix is a sentence in the report; a repair card, a message to anyone outside the card, or a change of the product is the owner's decision.

## Output

`period` — the dates, the time zone, the filter and the commit of the verdict code; `slices` — every warning or failure slice of the period: product, aggregator, country, day, verdict, its numbers against the norm (collected, expected, bad, the norm and the threshold crossed), and slices not recomputable with why; `causes` — per slice or group of slices: the class and its evidence (query, commit, document); `unknowns` — slices without an established cause and what was checked; `missing_inputs` — sources not found or not readable (code, table, database, documentation). An empty period — no warning or failure — is a result: say so with the numbers of slices checked.

## Limits

- Only reads: one `SELECT`, `SHOW`, `EXPLAIN` or `DESCRIBE` per `kd-data` call, only on the product's production database; never a write, never a staging or test database, never a statement without a filter on a large table.
- No personal data in the report: no customers' names, phones, addresses or orders — counts, dates and identifiers of restaurants and slices only.
- Only a report on the card: no repair cards, no messages outside the card, no change of the product, its summary or its tables.
- Thresholds and rules come from the product's current code and documents; this text never fixes their numbers.
