# Specialization: site-copy (v1)

## Purpose

Change the user-visible texts of one project for one board card — pages, interface strings, messages — in the repository copy, in every language of the project, without changing behavior.

## Inputs

The card names each of these; none is guessed:

- project — the repository and the part of it the texts belong to;
- pages or strings — the routes, components or message keys to change;
- audience — who reads these texts;
- goal — what the change should achieve for the reader;
- languages — every language the project ships; the change keeps them in parity.

## Procedure

- Check the inputs first. When one is missing or ambiguous: as the persistent agent, ask the owner on the card with `kd-work ask` before changing anything and wait for the answer; as a worker, change nothing and end with status "blocked", naming each missing input in `missing_inputs`.
- Find each text in the repository: message catalogs first, then templates and components; change the text where it lives, never by duplicating it.
- Every claim about the product the new text makes needs a source (a document, a file:line, the owner's words on the card); a claim without one is not written and is named in the notes.
- Keep the languages in parity: the same keys in every catalog, the same meaning in every language, placeholders and markup unchanged.
- Run the project's tests that read the catalogs and texts; commit to the current branch with a conventional commit message in English.
- The persistent agent opens the pull request itself by its own workflow; a worker returns its commit and never opens one.

## Output

`commit` — the SHA of the final commit, or null when nothing was committed; `files` — the files changed; `notes` — each changed text as before → after per language, the source of each product claim, the parity check and its result; `missing_inputs` — the inputs that were missing or unclear and the claims left out for lack of a source, empty when there were none.

## Limits

- Texts only: no change of behavior, layout, styles, tests or configuration beyond the strings themselves.
- No language is left behind: a text changed in one language is changed in all of them, or the change is not made.
- No new dependencies; no secrets, internal hosts or client data in a text.
