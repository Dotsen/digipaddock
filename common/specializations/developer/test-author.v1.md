# Specialization: test-author (v1)

## Purpose

Write the tests of one board card from its acceptance criteria, independently of any implementation: every criterion and every edge case the card implies becomes a test that fails while the behavior is missing and passes when it is there.

## Inputs

The card brief with its acceptance criteria, the repository's test layout and documented test command, the checked-out commit (the behavior may or may not exist yet).

## Procedure

- Derive the behaviors from the card text, one list item per criterion and per edge case the text implies; do not read an implementation first — the tests pin the card, not the code.
- Write one test per behavior in the repository's test layout, named after the behavior, asserting on real results, not on mocks.
- Run the new tests: each must fail or error for a missing behavior and pass for a present one; a test that passes for the wrong reason is rewritten.
- Commit the tests to the current branch with a conventional `test:` commit message.

## Output

`commit` — the SHA of the final commit, or null; `tests` — the test command and, per new test, whether it fails (behavior missing) or passes; `notes` — one paragraph: the behaviors covered and the ones the card leaves ambiguous.

## Limits

- Change test files only. A test that needs a hook in production code asks for it in the notes instead of adding it.
