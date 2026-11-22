# Skills and specializations: source files and use

This guide describes the implemented instruction registry. For the distinction between an agent, its type and its capabilities, see [Team model](team-model.md).

## Are skills prompts?

In this project, a skill is a versioned Markdown instruction text reused as part of the model's prompt. It contains professional rules, checklists and examples. A specialization is also an instruction text, but defines the task's procedure, required inputs and result contract. The agent supplies the identity and existing capabilities that execute the work.

For example, `implementer` describes how to implement a change and return evidence; `stateful-testing` adds rules for retries, concurrent writes and recovery. The same knowledge can accompany `reviewer` or `tester` without making them implementers.

The Markdown itself is not an executable tool, a model training update or an access grant. Registry validation, installation checks, capability channels and sandbox policy are executable code around those texts. The `e2e-testing` skill teaches browser-test practice; the separate `e2e.request` capability permits the fixed executor. Adding that skill alone cannot start a browser.

These are project registry entries, not automatically discovered native `SKILL.md` packages. Card descriptions and quoted customer requests remain task data; they cannot select new permissions or replace the checked instructions.

## Where to read and edit

All source paths below are relative to the repository root:

```text
common/specializations/
  specializations.json          # registry: types, files, versions, results, compatibility
  skills/*.v1.md                # reusable skill instruction texts
  analyst/*.v*.md               # analyst specialization procedures
  developer/*.v*.md             # developer specialization procedures
  sysadmin/*.v*.md              # sysadmin specialization procedures
```

The authoritative catalog is [specializations.json](../common/specializations/specializations.json). Each skill entry declares its version, file and conflicts. Each specialization declares its type, file/version, default selection, skill allowlist, result keys, existing capabilities required by `requires`, and worker limits. A specialization text has Purpose, Inputs, Procedure, Output and Limits sections; skill texts are reusable guidance.

The factory installs each agent's available texts under its configured agent directory:

```text
<agents_root>/<agent>/playbooks/spec/<name>.md
<agents_root>/<agent>/playbooks/skill/<name>.md
```

Edit the repository source and use the normal installation process. Editing only an installed copy creates drift and can refuse the next turn. Names, result fields and compatibility changes also need a registry update; changing a source text changes its recorded SHA-256 even if its version number is unchanged.

## Implemented skills

Entries below are v1 unless their row names another version. The registry remains authoritative if this catalog changes later.

| Skill and source | What it adds | Allowed specializations |
| --- | --- | --- |
| [frontend](../common/specializations/skills/frontend.v1.md) | UI conventions, responsive states, accessibility and frontend checks | implementer, ui-design, reviewer, tester |
| [python-backend](../common/specializations/skills/python-backend.v1.md) | Backend boundaries, validation, durable data and Python checks | implementer, test-author, reviewer, tester |
| [security-review](../common/specializations/skills/security-review.v1.md) | Trust boundaries, permissions, injection and refusal cases | implementer, reviewer |
| [stateful-testing](../common/specializations/skills/stateful-testing.v1.md) | State transitions, idempotency, stale revisions, concurrency and restart recovery | implementer, test-author, reviewer, tester |
| [localization](../common/specializations/skills/localization.v1.md) | Terminology, ru/en catalog parity, parameters, quantities and locale formatting | site-copy, docs, implementer, reviewer, tester |
| [e2e-testing](../common/specializations/skills/e2e-testing.v2.md) | User scenarios, meaningful browser assertions, artifacts and explicit unperformed checks | implementer, test-author, reviewer, tester |

For catalog and interpolation checks, use `python scripts/check_localization.py`. The current cabinet interpolates simple `{name}` parameters; unsupported ICU forms are not an available pluralization feature. Semantic translation review and rendered layout checks remain separate from key parity.

Stateful probes use isolated temporary data and named recovery boundaries. They do not stop live services or establish exactly-once external effects merely because a queue accepted a command.

## Added specializations

| Specialization and source | Agent type | Required existing capabilities | Result |
| --- | --- | --- | --- |
| [brief-review](../common/specializations/analyst/brief-review.v2.md) | analyst | repo.read | verdict, basis, findings, missing_inputs |
| [brief-critic](../common/specializations/analyst/brief-critic.v1.md) | analyst (worker only) | repo.read | verdict, findings, questions, missing_inputs |
| [code-critic](../common/specializations/developer/code-critic.v3.md) | developer (worker only) | git.develop | verdict, findings, questions |
| [release-check](../common/specializations/analyst/release-check.v2.md) | analyst | repo.read | verdict, basis, findings, checks, missing_inputs |
| [post-deploy](../common/specializations/sysadmin/post-deploy.v2.md) | sysadmin (operator) | host.read, servers.read | verdict, basis, checks, evidence, missing_inputs |
| [support-draft](../common/specializations/analyst/support-draft.v2.md) | analyst | board.read, docs.read | draft, sources, unknowns, questions, missing_inputs |
| [data-health](../common/specializations/analyst/data-health.v1.md) | analyst | board.read, data.read, docs.read, repo.read | period, slices, causes, unknowns, missing_inputs |
| [incident-triage](../common/specializations/sysadmin/incident-triage.v1.md) | sysadmin (operator) | host.read, servers.read | summary, evidence, hypotheses, impact, next, missing_inputs |
| [drift-audit](../common/specializations/sysadmin/drift-audit.v1.md) | sysadmin (operator) | host.read, servers.read | summary, drifts, unchecked |
| [capacity](../common/specializations/sysadmin/capacity.v1.md) | sysadmin (operator) | host.read, servers.read | summary, resources, database, forecast, recommendation, unchecked |
| [vpn-deploy](../common/specializations/sysadmin/vpn-deploy.v1.md) | sysadmin (operator) | host.read, servers.read, servers.execute | summary, steps, final_check, owner_steps, missing_inputs |
| [cost-report](../common/specializations/analyst/cost-report.v1.md) | analyst | usage.read | period, totals, summary, changes, savings, gaps |
| [commit-review](../common/specializations/analyst/commit-review.v1.md) | analyst | repo.read, board.read | basis, findings, summary, missing_inputs |
| [onboarding](../common/specializations/analyst/onboarding.v1.md) | analyst | board.read, docs.read | drafts, sources, unknowns, missing_inputs |

`brief-review` independently reviews another card's specification on a separate review card. It uses the checked prepare/record/status flow described in its instruction file. A changed scope, dependency, intended executor or source commit can make an old conclusion historical; the reviewer does not implement or start the source task. One analyst both writes and reviews briefs, so a review of an analyst-written brief is a fresh reading by the same role, not an independent one: `prepare` returns the brief's author and `same_role`, the round keeps it, and the instruction forbids relying on memory of the target. Minor (style) findings do not prevent `ready`.

`release-check` reviews an exact diff and operations/checks/rollback plan. `post-deploy` observes an authorized installation's actual version, services, bounded logs and affected scenario. Neither procedure approves installation or performs repair. Their verdicts distinguish success, known defects and missing essential evidence. See [Explicit release verification](release-checks.md), including the difference between validating proposed agent inputs with `--check` and comparing installed state with `--verify`.

`support-draft` prepares text for the owner to review and send. Material claims need readable sources with dates/versions. Unknown cause does not establish an ongoing investigation, unknown delivery status proves neither failure nor success, and a follow-up needs authorization. See [Support drafts](support-drafts.md). Its synthetic probes passed after a correction; real anonymized inquiry and owner content acceptance remain pending. Document numbers alone do not establish document contents.

`data-health` reviews a product's daily data-health summary for a period (by default the previous 7 days) on the production database through `kd-data`. The texts name no deployment value: the analyst finds the summary job, the verdict code with its thresholds, the daily table and the documented rule in the product's repository copy and recomputes each slice's verdict from that code, marking what the table cannot show as not recomputable. Each warning or failure slice gets a cause class — collection, parsing, configuration, a real change, or unknown — with its evidence (a query, a commit, a document). Reads only, no personal data, a report on the card only. The coordinator creates the weekly card on Mondays (its digest playbook); the owner may ask for one any day. Whether the recomputation matches the product's own summary is checked on the first weekly card.

`incident-triage` investigates one monitor incident P-n read-only. The cabinet sets `spec:incident-triage` on every `monitor:investigation` card it opens and on a card transferred to an investigation, replacing that card's previous `spec:` and `skill:` labels. Evidence names the server, the read and its time; hypotheses stay apart from facts; a refused read is reported as unknown. The next step is a recommendation: a server change still needs a plan the owner signs.

`drift-audit` and `capacity` read a server through the managed servers' read gate (`agent-ro-gate`) only; their texts name only reads that gate allows, and a test runs every named read through the gate's own check. A read the gate refuses, a missing channel or an unreadable document is reported under `unchecked`, never as a match or a measurement. `drift-audit` lists each drift as the document's statement, the observed fact and whether the document or the server should change. `capacity` forecasts only from at least two dated measurements; one snapshot gives no forecast. Neither changes a server or a document: a fix is a separate card, a server change a plan the owner signs.

`vpn-deploy` brings a new VPN server into service by the deployment's own checklist: the sysadmin's instructions name the document, and the text refers to its new VPN server checklist section by name without repeating a step, so the document stays the one source. A read-only pre-flight comes first, then one plan the owner signs (critical network changes as exact commands he approves), the steps in the checklist's order and the checklist's final check in full. Database records, keys, access and the server inventory stay the owner's: the sysadmin gives him the exact text. The result lists every checklist step with its status and evidence.

`commit-review` reviews one change at the owner's request: the card names the project and a commit sha, a branch (`origin/<branch>`) or a range (`<base>..<head>`). The analyst reads it through `kd-repo` only and returns findings with the file, the line, what is wrong, a failure scenario and the severity. A project or revision that does not resolve, or an empty range, goes to `missing_inputs`; another revision is never reviewed in its place. The review approves nothing and changes nothing: a fix is a separate card.

`onboarding` drafts material for a customer or partner who connects to a product — an instruction, a checklist, a letter — from the project documentation, every claim with its source. Its boundary with `support-draft`: connecting and the first steps, not an answer to an inquiry. The recipient's personal data and credentials stay placeholders; the owner reviews and sends.

`cost-report` reports the agents' spending over a period on a card the owner gives the analyst (by default the last 7 days, compared with the previous 7). The analyst holds `usage.read` for it: only `kd-usage show`, which prints the ledger's rows with a `cost` column — the row's `cost_usd`, an estimate at the cabinet's model prices, not a bill; `-` for a row without a price and for the memory server's rows. Days without data and rows without a price are named in the report. The analyst only reports and proposes savings; a change of models or budgets is the owner's.

## Selection and loading

The agent chooses its own specialization and compatible skills before task effects and when the work changes
stage. The host supplies a trusted catalog of its type's choices. The owner is not asked to choose internal
instructions. A suitable existing choice stays in place; a missing optional skill alone does not block work.
Product decisions, essential missing inputs and approval for effects remain the owner's decisions.

On its own Inbox, To Do, In Progress or Blocked card the agent can use `kd-task edit --spec <name>` together
with `--remove-label skill:<old>` and `--add-label skill:<new>` as needed, in one revision-checked edit. The CLI
checks installed bytes before changing the card and returns the new checked instruction texts for immediate
use in the current turn. It refuses stale copies, incompatible skills, another agent's card and a new
specialization on Review or terminal cards. The edit changes no scope, status, capability or effect approval.

Documentation-only work uses `docs` and the reusable `technical-writing` skill (structure, terminology,
source checks, examples and consistency across documents). `technical-writing` is also compatible with
`implementer` and `reviewer`: behavior-changing agent instructions are implementation, not documentation-only.

The existing task conversation shows separate compact blocks for changed instruction choices and for checked
texts delivered to a run's context. Versions are visible; global navigation and ordinary message styling stay
unchanged. Hindsight tool calls show their operation and observed state: running, returned, failed or result
not received. A returned retrieval does not prove its sources informed the answer; a returned write does not
prove indexing finished. Queries and source contents stay out of these activity blocks. Automatic Hindsight
hook injection is not inferred from tool calls, and no events is not proof that memory was unused.

One card selects one specialization with `spec:<name>`, or the type's default when available. Select zero to two skills with `skill:<name>` labels on a persistent card or repeated `--skill <name>` arguments in a worker request. The specialization's skill list is an allowlist, not an instruction to load every listed skill. Unknown, duplicate, incompatible, conflicting and excess selections are refused.

| Task | Agent | Specialization | Selected skills |
| --- | --- | --- | --- |
| Implement safe retry behavior | dev | implementer | python-backend, stateful-testing |
| Change bilingual UI labels | dev | implementer | frontend, localization |
| Request the prepared browser smoke | dev | tester | frontend, e2e-testing |
| Check a technical translation | dev | docs | localization |
| Independently review a brief | analyst | brief-review | none |
| Prepare a sourced customer draft | analyst | support-draft | none |

For a worker, [worker_packs.py](../common/kd/worker_packs.py) composes the system prompt as the runtime frame, specialization text and explicitly selected skill texts. The card brief is a separate task message. The runner independently enforces the worker policy and records exact text versions/hashes.

For a persistent card, [persistent_skills.py](../common/kd/persistent_skills.py) rechecks the current selection and installed bytes before every turn, including resume. It supplies the checked texts and their installed paths with the current provenance. Changed provenance is recorded on the card; the per-turn preamble instructs the agent to read changed installed instructions before task effects. A stale or altered installed text refuses the turn until the normal reinstall restores it.

The shared selection and registry validator is [specializations.py](../common/kd/specializations.py). The Markdown guides the model's behavior; these Python checks and the existing executor boundaries enforce selection, integrity and authority. Registration alone does not prove instruction quality: representative tasks and review of the actual output remain necessary.

## Execution availability and acceptance

The repository's [worker slot configuration](../common/worker/worker-slots.json) declares two developer slots and two analyst slots. The analyst's pool runs the critics of a brief's [council](cabinet.md#the-council-of-models): `brief-critic` jobs the cabinet starts itself, never a specialization to assign on a card. The developer's pool runs the critics of a pull request's council the same way — `code-critic` jobs on the project's copy at the pull request's head, beside the jobs the coordinator orders. A worker has no reader channel, so such a job carries what it reads — the card as files of its slot's `inputs/` folder — and reads the repositories by its agent's grant. A registry entry for sysadmin does not create slots for that type. Current unsupported requests are refused; a supported unattended run missing essential facts or tools returns blocked with missing inputs. Persistent analyst and sysadmin sessions are the demonstrated path for the other specializations.

The browser capability accepts only the prepared `cabinet-smoke` suite through `kd-e2e`, not arbitrary URLs, shell commands or launch flags. It uses synthetic data in an isolated runtime. Missing preparation is blocked, and workers have no executor channel. See [Prepared end-to-end runtime](e2e-runtime.md) for preparation, the execution boundary and artifacts. This fixed capability does not implement a general browser capability.

A deployment records its acceptance probes of these texts in its own internal protocol; one set of observed runs is no proof for a different commit, environment or customer incident.
