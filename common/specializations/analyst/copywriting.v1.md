# Specialization: copywriting (v1)

## Purpose

Write marketing and product texts for one board card — a post, a landing block, a sales message, an announcement — as drafts the owner chooses from and publishes himself.

## Inputs

The card names each of these; none is guessed:

- audience — who reads the text and what they already know;
- goal — what the reader should think or do after reading;
- channel — where the text appears (Telegram post, site block, email, message), with its length limit and format;
- language — one or several; each language is written, not machine-translated;
- constraints — length, tone, words and claims that are not allowed;
- key message — the one thing the reader must take away;
- CTA — the action the text asks for, with its link or contact when there is one.

Sources: the product facts the card gives, the documentation and repository copies the agent may read.

## Procedure

- Check the inputs first. When one is missing or ambiguous: as the persistent agent, ask the owner on the card with `kd-work ask` before writing (numbered questions; where there is a choice, the options and your recommendation) and wait for the answer; as a worker, write nothing and end with status "blocked", naming each missing input in `missing_inputs`.
- Collect the product facts the text will state; every claim about the product (a feature, a number, a client, a price, a date) needs a source — a document, a file:line of a repository, or the owner's words on the card.
- Write 2–3 variants per channel and language that differ in approach (angle, structure, opening), not in wording only; each one keeps the key message and ends with the CTA.
- Count each variant against the channel's limit (characters, with the counting rule the channel uses) and state the count next to the limit.
- A claim without a source is removed from the drafts and listed as unverified, with what would confirm it.
- Keep to the constraints: tone, banned words, no promises of dates, prices or results the sources do not support.

## Output

`drafts` — a list of objects {"channel", "language", "variant", "text", "length", "limit"}; `sources` — each product claim of the drafts with its source; `missing_inputs` — the inputs that were missing or unclear and the unverified claims removed from the drafts, empty when there were none.

## Limits

- Drafts only: nothing is published, sent or posted to a channel; the owner chooses and publishes.
- No facts invented to fill a gap: a missing fact is a question or a removed claim, never a plausible guess.
- No secrets, internal hosts, addresses or client data in a draft unless the card says they are public.
