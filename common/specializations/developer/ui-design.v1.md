# Specialization: ui-design (v1)

## Purpose

Design the screens of one board card for an existing web interface: their states, their behavior at each width, the design tokens they use and the rules of their components, delivered as a design document and an HTML/CSS prototype in the repository copy.

## Inputs

The card names each of these; none is guessed:

- screens — the routes or components to design, and the repository copy they live in;
- scenarios — the tasks a person does on these screens, step by step;
- widths — 360, 900 and 1440 px unless the card names others.

## Procedure

- Check the inputs first. When one is missing or ambiguous: as the persistent agent, ask the owner on the card with `kd-work ask` before designing and wait for the answer; as a worker, design nothing and end with status "blocked", naming each missing input in `missing_inputs`.
- Read the current screens: their components, styles, the global stylesheet and its custom properties, the message catalogs; list every literal value (colour, size, spacing, radius, font) the screens use.
- Design every state of each screen: normal, empty, loading, error with a retry, long text, many items; describe what each shows and what the person can do there.
- Describe the behavior at each width: what moves, folds, scrolls or hides, and where the main action stays.
- Define the tokens of these screens by role, reusing the existing custom properties first; give the replacement map — each literal found → its token — and both themes where the app has them.
- Write the component rules: sizes, spacing, states (hover, focus, disabled, active), the text each one carries.
- Put the design document and a static HTML/CSS prototype of the screens in `docs/prototypes/KD-<n>/` (the card's number), the prototype using only the tokens; commit to the current branch with a conventional `docs:` commit message in English.
- The persistent agent opens the pull request itself by its own workflow; a worker returns its commit and never opens one.

## Output

`commit` — the SHA of the final commit, or null when nothing was committed; `files` — the files added or changed; `tokens` — the tokens of these screens with their values and the replacement map of literals; `screens` — each screen with its states and its behavior at each width; `missing_inputs` — the inputs that were missing or unclear, empty when there were none.

## Limits

- Design only: product code, styles and tests stay unchanged; everything is written under `docs/prototypes/KD-<n>/`.
- The prototype is static HTML and CSS: no frameworks, no scripts that fetch data, no new dependencies.
- Design what the card names: a wider change (another screen, the whole token system) is proposed in the document, not made.
