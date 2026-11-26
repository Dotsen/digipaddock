# Cabinet design tokens

Every screen of the [cabinet](cabinet.md) takes its colours, type sizes, spaces and corners from one set of tokens:
the custom properties at the top of `common/cabinet/web/src/app.css`. A component's styles name a token
(`font-size: var(--fs-md)`, `padding: var(--sp-8) var(--sp-12)`), not a number, so one change in `app.css` reaches
every screen, and two screens that mean the same thing look the same.

## The scales

**Colour.** The dark palette (`--bg`, `--surface`, `--surface-2`, `--surface-3`, `--line`, `--line-soft`, `--text`,
`--muted`) with one accent (`--accent`, its ink `--accent-ink`), the states (`--good`, `--warn`, `--crit`, `--info`,
each with its `-bg`), the authors' tints (`--au-*`) and a hue per project (`--project-0` … `--project-5`, picked from
the project's id; none is a state's green, amber or red), and what lies over the page: `--scrim` (a dialog's),
`--scrim-deep` (a picture's), `--shadow-pop` (a popup menu), `--shadow-float` (a chip floating over a list). The
light palette is laid down but not switched on.

**Type** — one size per role:

| Token | Size | For |
|---|---|---|
| `--fs-tiny` | 10px | a caret, a tab bar's label, a chart's tick |
| `--fs-xs` | 11px | a panel's overline |
| `--fs-sm` | 12px | a chip, an id, meta, a section's header |
| `--fs-md` | 13px | secondary text, a hint, a small button, a field's label |
| `--fs-ui` | 14px | a button, a tab, a menu entry, a title in a list |
| `--fs-body` | 15px | text |
| `--fs-lead` | 16px | a panel's title, the board's project chips |
| `--fs-title` | 17px | a sheet's or a section's heading |
| `--fs-heading` | 20px | a card's title, a document's first heading |
| `--fs-page` | 22px | a page's title |
| `--fs-display` | 30px | a big number |

Weights are 400, 500 (a control, a chip), 600 (a heading, a primary button) and 700 (a page's title).

**Space** — gaps, paddings and margins on a 2px grid: `--sp-2`, `--sp-4`, `--sp-6`, `--sp-8`, `--sp-10`, `--sp-12`,
`--sp-14`, `--sp-16`, `--sp-20`, `--sp-24`, `--sp-32`, `--sp-40` (the number is the size in pixels).

**Corners**: `--r-xs` 4px (a mark), `--r-sm` 6px (a code block, a small box), `--r-md` 8px (a button, a field, a
tile), `--r-lg` 12px (a panel; `--radius` is the same), `--r-xl` 16px (a sheet), `--r-pill` (a chip, a pill).

## Shared pieces

Before a component styles its own, `app.css` has: `.btn` (one size for every action — a screen does not enlarge its
own; `.small` the other; `.primary`, `.ghost`,
`.danger`, `.wide`), `.iconbtn`, `.chip` (with `.good`, `.warn`, `.crit`, `.acc`, `.info`, `.text`), `.panel`,
`.section-h`, `.note`, `.banner`, `.field`, `.segmented`, `.tabs`, `.sheet`, `.topbar`. Components:
`ProjectChip` (a project in its colour), `StatusPill`, `Avatar`, `Badge`.

## The check

`src/design.test.ts` (part of the frontend suite) counts, per file, the numbers still written where a token belongs —
font sizes, corners and spaces in pixels, weights off the four, colours outside the palette — against
`common/cabinet/web/design-baseline.json`. The count may only go down: a new number fails the suite, and so does a
baseline left higher than a file now has (lower it with
`KD_DESIGN_BASELINE=write npx vitest run src/design.test.ts`). A size that has no token yet is a new token in
`app.css` and a line here, not a number in a component. The screens move onto the tokens one at a time, each with its
screenshots at 390 and 1280 px before and after; `e2e/card-look.spec.ts` takes the card's page in five states.
