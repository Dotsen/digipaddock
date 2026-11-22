# Specialization: docs (v1)

## Purpose

Update the documentation of one board card in the repository copy: README, docs/, docstrings and comments, without changing behavior.

## Inputs

The card brief, the repository's language rules (its CLAUDE.md), the code the documentation describes.

## Procedure

- Read the code the card names and the documentation that describes it; change the documentation to match the code, never the other way round.
- Match the language rules of the repository (code and comments in English; owner-facing text as the repository says).
- Commit to the current branch with a conventional `docs:` commit message.

## Output

`commit` — the SHA of the final commit, or null; `files` — the files changed; `notes` — one paragraph: what was documented and what the code left unclear.

## Limits

- Documentation only: no change of behavior, tests or configuration.
