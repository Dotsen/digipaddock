# Brief template (card description)

Write the card description and acceptance criteria in the owner's language (`owner_language`). Use `kd-task edit KD-<n> --revision <rev> --description '…'` with the revision from `kd-task view`. On exit code 3, reread the card before editing again. Pass one `--ac '…'` per acceptance criterion.

The cabinet renders the description as Markdown.

- Leave a blank line before each heading and underline the heading with `---`. The command rejects a line beginning with `#` inside its argument.
- Put technical names in backticks, including paths, routes, tables, columns, fields, variables, commands and flags, response codes, and literal values. Leave card numbers such as KD-<n> plain so the cabinet makes them links.
- Use one line per list item and a table for comparisons.
- Use a diagram when steps, exchanged calls, or states are easier to see, usually in Approach or Current behavior. Open the block with `` ```mermaid ``, on its own line outside a list. Use `flowchart TD`, `sequenceDiagram`, or `stateDiagram-v2` with short labels and up to about ten nodes, arranged top to bottom. The cabinet displays invalid diagrams as text. Keep the decisions in the accompanying prose.
- Pass text containing backticks in single quotes. Use ’ for an apostrophe inside.

Use the sections in this order. For a Russian-speaking owner write the headings exactly as the table under «Russian headings» gives them, so every brief reads the same. Leave out a section marked optional when it has nothing to say.

```
Goal
---
<In one sentence, say what should become possible or stop breaking and for whom.>

Agreed work
---
<State the agreement with the owner in the affirmative. List separate changes.>

Scope
---
<What the card covers.>

Out of scope
---
<What the card deliberately leaves out.>

Current behavior
---
<How it works now, based on the code and docs. Include module paths.>

Approach
---
<What changes and where, in 3–7 lines. Name rejected approaches and why. Diagram flows or states where useful.>

Affected modules
---
<List files, components, units, and pages.>

Risks
---
<One item per risk. Say what might break, how to detect it, and how to roll back.>

Related cards
---
<KD-<n> and the shared module, mechanism or limit, plus the order of work. Optional.>

Hypotheses and probes
---
<For unmeasured speed, memory, quality or availability, say what to probe before implementation and what decision depends on it. Optional.>

Owner decisions
---
<One dated item per decision from the owner's answers. Optional.>

Defaults chosen
---
<One item per decision the analyst made from a clear recommendation, with its rationale. The owner can change it. Optional.>

Open questions
---
<What remains open, who will decide it, and when. Optional.>

Size
---
S | M | L (<One sentence of rationale.>)

Operations
---
<One item for each factory step needed after rollout. Optional.>

Stages
---
<For independent parts of an L brief, give each stage's key, role, spec, title, and prerequisites. Optional.>

Next step
---
@<assignee>, `spec:<name>` (<Why this specialization fits.>).

Agreed with the owner on <date>. Answers are in the card thread.
```

**Size.** S is one small PR by one assignee, with no operations, stages, server work, production writes, secrets, security, or money. An eligible S brief starts automatically. M fits one card. L is large. When in doubt, use the larger size. Include the corresponding `size:S|M|L` label in the handoff.

**Acceptance criteria.** Each criterion describes an outcome verifiable by a command, a page, or a test, never just “works correctly”. Cover every substantive part of the goal. For independent parts of large work, use `--stages`, not a criterion such as “subtasks created”. The parent criteria cover the whole outcome and stage criteria cover their own part.

**Stages.** List the stages in the description and store them separately with `kd-task edit KD-<n> --revision <rev> --stages '<JSON>'`. For example:

```json
[
  {"key": "api", "title": "Publishing API", "role": "@dev", "spec": "implementer", "depends": [], "description": "…", "acceptance": ["…"]},
  {"key": "ui", "title": "Publishing screen", "role": "@dev", "spec": "implementer", "depends": ["api"], "description": "…", "acceptance": ["…"]}
]
```

Use at most five stages. A stable `key` starts with a lowercase ASCII letter and then uses lowercase ASCII letters, digits, or hyphens, up to 32 characters total. A `title` is at most 120 characters. The `role` is `@dev` or `@sysadmin` and `spec` belongs to that role. `depends` lists earlier keys and `acceptance` contains 1–20 criteria. Set the optional `project` if a stage belongs to a different board project.

Approval starts one child card per stage. Children without dependencies start in To Do and dependent children start in Blocked with the dependency links. The parent shows the plan and moves to Review after the final child is Done. A stage edit after `brief-ready` requires a new revision and approval. Existing child cards stay in place. A staged brief cannot carry `component.install` with `"revision": "release"`. For that operation the developer opens a factory plan on the stage card named by its PR.

**Operations.** List the operations in the description and store the typed steps separately with `kd-task edit KD-<n> --revision <rev> --operations '<JSON>'`. Use the factory catalog. The cabinet validates the steps. Write their explanations in the owner's language.

```json
[
  {"verb": "agent.reinstall", "target": "factory", "agents": ["dev"], "what": "…", "effect": "…", "risk": {"level": "low", "why": "…"}, "rollback": "…"}
]
```

To remove a step, submit the list without it. To remove all steps, pass `--operations '[]'`. The owner's brief approval covers these steps and `@dev` executes them. Server work uses a sysadmin plan.

For a component step needed just after rollout, use an entry such as `{"verb": "component.install", "target": "factory", "component": "repo-attach", "mode": "check", "revision": "release", "what": "…", "effect": "…", "risk": {"level": "low", "why": "…"}, "rollback": "…"}`. The card's PR release requests that step at its released revision under the brief approval. The catalog lists `repo-attach`, `transcribe`, `e2e-runtime`, and `devcheck` with `install` or `check`, and `session-host` with `check` or `restart`.

**Final check.** Follow the final check in `playbooks/spec/task-brief.md` before handoff. Verify the code, consistency of decisions and sections, related cards, end-to-end scenario, limits, errors and recovery, measurable claims, feasibility, and acceptance coverage. Fix any gap and repeat the check. Record the outcome in a short note in the owner's language.

## Russian headings

| Section | Heading for a Russian-speaking owner |
|---|---|
| Goal | Цель |
| Agreed work | Что делаем |
| Scope | Охват |
| Out of scope | Вне охвата |
| Current behavior | Сейчас |
| Approach | Подход |
| Affected modules | Затронутые модули |
| Risks | Риски |
| Related cards | Связанные карточки |
| Hypotheses and probes | Гипотезы и пробы |
| Owner decisions | Решения владельца |
| Defaults chosen | Решено по умолчанию |
| Open questions | Не решено |
| Size | Размер |
| Operations | Операции |
| Stages | Этапы |
| Next step | Следующий шаг |

The closing line is `Согласовано с владельцем: <дата>, ответы на вопросы — в треде карточки.`; the final-check note starts with `Финальная проверка: …`.
