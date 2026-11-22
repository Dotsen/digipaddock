<script lang="ts">
  // A council of models in the card's thread, one block per gathering: what it reads and which round it
  // is in, how it stands, then round by round each critic as a card of its own — the model, the verdict, how many
  // findings of each weight, what the read cost, a mark when the critic read on the writer's own model — with its
  // findings and its questions. The current round is open, earlier ones folded, each saying what it found.
  // A finding's row says what goes wrong, with where under it; how grave it is and how its writer
  // answered are icons, named on hover and for a reader — and in words once the finding is opened, with the
  // evidence, the recommendation and the answer. The gravest come first. The writer answers every finding
  // , but a minor one holds nothing: left without an answer, its row carries no answer mark (a
  // dash there read as «rejected») and says so in words once opened. The writer answers a round on its own
  // findings, so a round's line counts, beside what
  // was found by weight, what became of it: fixed, rejected, moved to later, still without an answer.
  // What a critic had no access to is listed apart from its questions.
  // The icons are explained once, under "Legend": a phone has no hover.
  // Whoever manages councils (`settings`) cancels an open one — asked again here, in the block — or opens the
  // composition: a change holds from the next round.
  // A council of the code names its pull request with a link to the forge, and each round the head it
  // read; the pull request's decision links to this block by its id. While it is open the decision is held back,
  // so the block shows the pull request itself — the files, the cage, the diff — with nothing to decide.
  // What a critic wrote is shown as plain text: it is a model's output, never markup of ours.
  import {
    answerState, bySeverity, isOpen, OUTCOMES, outcomes, runText, SEVERITIES, split, standing, stateText, totals,
    writerOf, type AnswerState, type Outcome, type Severity,
  } from "../lib/council";
  import { t, type Key } from "../lib/i18n.svelte";
  import { reasonText } from "../lib/runReason";
  import { models } from "../lib/stores/models.svelte";
  import type { CouncilFinding, CouncilRound, CouncilRun, CouncilView } from "../lib/types";
  import PrBody from "./PrBody.svelte";

  let {
    council,
    canManage = false,
    busy = false,
    assignee = "",
    oncancel,
    onedit,
  }: {
    council: CouncilView;
    /** The person may cancel the council and change its composition. */
    canManage?: boolean;
    busy?: boolean;
    /** The card's agent: the writer of what a code council reads. */
    assignee?: string;
    oncancel?: () => void;
    onedit?: () => void;
  } = $props();

  const open = $derived(isOpen(council));
  const version = $derived(council.rounds_list.at(-1)?.version ?? council.basis.version ?? "");
  const code = $derived(council.kind === "code");
  const title = $derived(
    t(`council.block.title.${council.kind}` as Key, {
      version, number: council.basis.number ?? "", round: council.round, rounds: council.rounds,
    }),
  );
  const said = $derived(stateText(standing(council), writerOf(council.kind, assignee), council.kind));
  const TONE: Record<string, string> = { running: "info", needs_writer: "info", clean: "good", settled: "warn", limited: "warn", failed: "crit" };
  let confirming = $state(false);

  const roundTitle = (entry: CouncilRound) =>
    entry.version ? t("council.block.round", { round: entry.round, version: entry.version })
    : entry.head ? t("council.block.round_head", { round: entry.round, sha: entry.head.slice(0, 7) })
    : t("council.block.round_plain", { round: entry.round });
  const runTone = (run: CouncilRun) =>
    run.state === "failed" ? "crit" : run.state !== "done" ? "" : run.verdict === "ready" ? "good" : "warn";
  const cost = (value: number) => (value >= 0.005 ? `$${value.toFixed(2)}` : "<$0.01");
  /** the models that served the critic's job — when that says more than the critic's own name. */
  const servedText = (run: CouncilRun) => {
    const text = models.served(run.served ?? []);
    return text === models.name(run.model) ? "" : text;
  };
  const failure = (run: CouncilRun) => (run.state === "failed" ? run.fail_reason : "");
  const hasFacts = (run: CouncilRun) =>
    Boolean(failure(run)) || SEVERITIES.some((severity) => run.counts[severity]) || run.cost > 0 || run.same_model;
  const severityText = (severity: Severity) => t(`council.severity.${severity}` as Key);
  const answerText = (state: AnswerState) => t(`council.answer.${state}` as Key);
  const answerLabel = (finding: CouncilFinding) => t("council.answer.label", { answer: answerText(answerState(finding)) });
  const anyFinding = $derived(council.rounds_list.some((entry) => entry.runs.some((run) => run.findings.length > 0)));

  function cancel() {
    confirming = false;
    oncancel?.();
  }
</script>

<!-- the weight by shape as well as colour: a filled disc, a warning triangle, a small ring — none of them an arrow,
     which on a row that opens would read as «unfold» -->
{#snippet severityIcon(severity: Severity)}
  <svg class="ico" viewBox="0 0 16 16" aria-hidden="true">
    {#if severity === "critical"}<circle class="disc" cx="8" cy="8" r="6.5" /><path class="cut" d="M8 4.6v4M8 11.2v.3" />
    {:else if severity === "major"}<path d="M8 2.6l5.8 10.2H2.2z" /><path d="M8 6.6v2.8M8 11.2v.2" />
    {:else}<circle cx="8" cy="8" r="3" />{/if}
  </svg>
{/snippet}

{#snippet answerIcon(state: Outcome)}
  <svg class="ico" viewBox="0 0 16 16" aria-hidden="true">
    {#if state === "fixed"}<path d="M3.5 8.5l3 3 6-7" />
    {:else if state === "rejected"}<path d="M4.5 4.5l7 7M11.5 4.5l-7 7" />
    {:else if state === "later"}<path d="M3 8h9.5M9 4.5L12.5 8 9 11.5" />
    {:else}<circle cx="8" cy="8" r="5.5" /><path d="M8 5v3.2l2 1.3" />{/if}
  </svg>
{/snippet}

<!-- how many findings of each weight; `words` — with the weight's short name beside the icon (a critic's card) -->
{#snippet counts(sum: Record<Severity, number>, words: boolean)}
  {#each SEVERITIES as severity (severity)}
    {#if sum[severity]}
      {@const label = `${severityText(severity)}: ${sum[severity]}`}
      <span class="count {severity}" role="img" title={label} aria-label={label}>
        {@render severityIcon(severity)}{#if words}{t(`council.count.${severity}` as Key)}{" "}{/if}{sum[severity]}
      </span>
    {/if}
  {/each}
{/snippet}

<!-- what became of a round's findings: how many the writer fixed, rejected, moved to later, left without an answer -->
{#snippet tally(sum: Record<Outcome, number>)}
  {#if OUTCOMES.some((state) => sum[state])}
    <span class="tallies">
      {#each OUTCOMES as state (state)}
        {#if sum[state]}
          {@const label = `${answerText(state)}: ${sum[state]}`}
          <span class="tally {state}" role="img" title={label} aria-label={label}>{@render answerIcon(state)}{sum[state]}</span>
        {/if}
      {/each}
    </span>
  {/if}
{/snippet}

<section class="council {TONE[council.state] ?? ''}" aria-label={title} id="council-{council.id}">
  <header class="head">
    <div class="ttl">{title}</div>
    <div class="sub">
      {said}
      {#if code && council.basis.url}· <a href={council.basis.url} target="_blank" rel="noopener noreferrer">{t("decision.pr.forge")}</a>{/if}
    </div>
  </header>
  {#if council.pull}
    <details class="round pull">
      <summary>{t("council.block.held")}</summary>
      <PrBody
        id={council.id}
        view={council.pull}
        diffUrl="/api/cards/{encodeURIComponent(council.card)}/councils/{council.id}/diff"
      />
    </details>
  {/if}
  {#each council.rounds_list as entry (entry.round)}
    <details class="round" open={entry.round === council.round}>
      <summary>
        <span class="rt">{roundTitle(entry)}</span>
        {@render counts(totals(entry), false)}
        {@render tally(outcomes(entry))}
        {#if entry.state === "superseded"}<span class="muted">{t(code ? "council.round.superseded.code" : "council.round.superseded")}</span>{/if}
      </summary>
      <ul class="runs">
        {#each entry.runs as run (run.n)}
          {@const asks = split(run.questions)}
          <li class="run">
            <header class="critic">
              <div class="who">
                <b class="name">{models.name(run.model)}</b>
                {#if servedText(run)}<span class="muted served" title={t("council.served", { names: (run.served ?? []).join(", ") })}>{servedText(run)}</span>{/if}
                <span class="chip verdict {runTone(run)}">{runText(run)}</span>
              </div>
              {#if hasFacts(run)}
                <div class="facts">
                  {#if failure(run)}<span class="why" title={run.fail_reason}>{reasonText(run.fail_reason, run.fail_reason)}</span>{/if}
                  {@render counts(run.counts, true)}
                  {#if run.cost > 0}<span class="muted">{cost(run.cost)}</span>{/if}
                  {#if run.same_model}<span class="chip warn" title={t("council.same_model_title")}>{t("council.same_model")}</span>{/if}
                </div>
              {/if}
            </header>
            {#if run.findings.length}
              <ul class="findings">
                {#each bySeverity(run.findings) as finding (finding.id)}
                  {@const state = answerState(finding)}
                  <li>
                    <details class="finding">
                      <summary>
                        <span class="sev {finding.severity}" role="img" title={severityText(finding.severity)} aria-label={severityText(finding.severity)}>
                          {@render severityIcon(finding.severity)}
                        </span>
                        <span class="txt">
                          <span class="what">{finding.scenario || finding.recommendation}</span>
                          <span class="where"><span class="lbl">{t("council.finding.where")}:</span> {finding.location}</span>
                        </span>
                        {#if state !== "optional"}
                          <span class="ans {state}" role="img" title={answerLabel(finding)} aria-label={answerLabel(finding)}>
                            {@render answerIcon(state)}
                          </span>
                        {/if}
                      </summary>
                      <div class="body">
                        <p class="meta">{severityText(finding.severity)} · {finding.id}</p>
                        <dl>
                          <dt>{t("council.finding.evidence")}</dt>
                          <dd>{finding.evidence}</dd>
                          <dt>{t("council.finding.recommendation")}</dt>
                          <dd>{finding.recommendation}</dd>
                          <dt>{t("council.finding.answer")}</dt>
                          <dd class="answer {state}">
                            {answerText(state)}{#if finding.card}{" "}<a href="/cards/{encodeURIComponent(finding.card)}">{finding.card}</a>{/if}{#if finding.note}: {finding.note}{/if}
                          </dd>
                        </dl>
                      </div>
                    </details>
                  </li>
                {/each}
              </ul>
            {/if}
            {#if asks.asked.length}
              <div class="asks">
                <p class="qh">{t("council.questions")}</p>
                <ul class="questions">
                  {#each asks.asked as question, i (i)}
                    <li>
                      {question.text}
                      {#if question.options?.length}
                        <ul class="options">{#each question.options as option, k (k)}<li>{option}</li>{/each}</ul>
                      {/if}
                      {#if question.recommendation}
                        <span class="muted">{t("council.question.recommends", { text: question.recommendation })}</span>
                      {/if}
                    </li>
                  {/each}
                </ul>
              </div>
            {/if}
            {#if asks.missing.length}
              <div class="asks">
                <p class="qh">{t("council.missing")}</p>
                <ul class="missing">
                  {#each asks.missing as question, i (i)}<li>{question.text}</li>{/each}
                </ul>
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    </details>
  {/each}
  {#if anyFinding}
    <details class="legend">
      <summary>{t("council.legend")}</summary>
      <ul>
        {#each SEVERITIES as severity (severity)}
          <li><span class="sev {severity}">{@render severityIcon(severity)}</span>{severityText(severity)}</li>
        {/each}
      </ul>
      <ul>
        {#each OUTCOMES as state (state)}
          <li><span class="ans {state}">{@render answerIcon(state)}</span>{answerText(state)}</li>
        {/each}
      </ul>
      <p>{t("council.legend.rule")}</p>
      <p>{t("council.legend.round")}</p>
    </details>
  {/if}
  {#if canManage && open}
    <div class="act">
      {#if confirming}
        <span class="ask">{t(code ? "council.cancel_confirm.code" : "council.cancel_confirm")}</span>
        <button type="button" class="btn small danger" disabled={busy} onclick={cancel}>{t("council.cancel_yes")}</button>
        <button type="button" class="btn small ghost" onclick={() => (confirming = false)}>{t("council.cancel_no")}</button>
      {:else}
        <button type="button" class="btn small" disabled={busy} onclick={() => (confirming = true)}>{t("council.cancel")}</button>
        {#if onedit}
          <button type="button" class="btn small ghost" disabled={busy} onclick={onedit}>{t("council.edit")}</button>
          <span class="muted hint">{t("council.edit_hint")}</span>
        {/if}
      {/if}
    </div>
  {/if}
</section>

<style>
  .council { border: 1px solid var(--line); border-left: 3px solid var(--line); border-radius: var(--radius); background: var(--surface); overflow: hidden; font-size: 14px; }
  .council.info { border-left-color: var(--info); }
  .council.good { border-left-color: var(--good); }
  .council.warn { border-left-color: var(--warn); }
  .council.crit { border-left-color: var(--crit); }
  .head { padding: 12px 14px 8px; display: flex; flex-direction: column; gap: 4px; }
  .ttl { font-weight: 700; font-size: 16px; overflow-wrap: anywhere; }
  .sub { color: var(--muted); font-size: 13px; }
  .sub a { color: var(--accent); text-decoration: none; }
  .muted { color: var(--muted); font-size: 13px; }
  .ico { width: 16px; height: 16px; flex: none; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
  .ico .disc { fill: currentColor; stroke: none; }
  .ico .cut { stroke: var(--surface); }
  .critical { color: var(--crit); } .major { color: var(--warn); } .minor { color: var(--muted); }

  .round { border-top: 1px solid var(--line); }
  .round > summary { padding: 8px 14px; cursor: pointer; color: var(--muted); font-size: 13px; }
  .round > summary > * { vertical-align: middle; }
  .rt { margin-right: 6px; }
  .count { display: inline-flex; align-items: center; gap: 2px; font-size: 13px; white-space: nowrap; margin-right: 8px; }
  /* beside what was found — what became of it: one outlined group, which stays whole when a narrow line wraps */
  .tallies { display: inline-flex; gap: 8px; padding: 1px 8px; margin-right: 8px; border: 1px solid var(--line); border-radius: 999px; }
  .tally { display: inline-flex; align-items: center; gap: 2px; font-size: 13px; white-space: nowrap; }
  .runs, .findings, .questions, .options, .missing { list-style: none; margin: 0; padding: 0; }
  .runs { display: grid; gap: 12px; padding: 2px 14px 14px; }

  /* a critic: a card of its own, its name the heading — where one critic ends and the next begins */
  .run { border: 1px solid var(--line); border-radius: calc(var(--radius) - 2px); overflow: hidden; min-width: 0; }
  .critic { background: var(--surface-2); padding: 10px 12px; display: grid; gap: 6px; }
  .who { display: flex; gap: 6px 10px; flex-wrap: wrap; align-items: center; }
  .name { font-size: 15px; font-weight: 700; overflow-wrap: anywhere; }
  .served { font-size: 12px; }
  .verdict { margin-left: auto; background: var(--surface); }
  .facts { display: flex; gap: 6px 4px; flex-wrap: wrap; align-items: center; }
  .facts > .muted { margin-right: 8px; }
  .why { color: var(--crit); font-size: 13px; overflow-wrap: anywhere; flex-basis: 100%; }

  .findings > li { border-top: 1px solid var(--line-soft); }
  .finding > summary { display: grid; grid-template-columns: 16px minmax(0, 1fr) 16px; gap: 10px; align-items: start; padding: 9px 12px; cursor: pointer; list-style: none; }
  .finding > summary::-webkit-details-marker { display: none; }
  .finding > summary:hover, .finding[open] > summary { background: var(--surface-2); }
  .sev, .ans { display: inline-flex; margin-top: 2px; }
  .txt { display: grid; gap: 2px; min-width: 0; }
  .what { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden; overflow-wrap: anywhere; }
  .where { color: var(--muted); font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  /* both spellings are released: the plain `line-clamp` holds on any block, whatever its display */
  .finding[open] .what { display: block; -webkit-line-clamp: unset; line-clamp: none; }
  .finding[open] .where { white-space: normal; overflow-wrap: anywhere; }
  .ans, .tally { color: var(--muted); }
  .fixed:is(.ans, .tally) { color: var(--good); } .rejected:is(.ans, .tally) { color: var(--text); } .none:is(.ans, .tally) { color: var(--warn); }
  .body { padding: 2px 12px 12px 38px; background: var(--surface-2); }
  @media (max-width: 640px) { .body { padding-left: 12px; } }
  .meta { margin: 0 0 4px; font-size: 12px; color: var(--muted); }
  .body dl { margin: 0; display: grid; gap: 2px; }
  .body dt, .qh { font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); }
  .body dt { margin-top: 6px; }
  .body dd { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
  .body dd a { color: var(--accent); text-decoration: none; }
  .answer.fixed { color: var(--good); } .answer.none { color: var(--warn); } .answer.optional { color: var(--muted); }

  .asks { border-top: 1px solid var(--line-soft); padding: 10px 12px; display: grid; gap: 6px; }
  .qh { margin: 0; }
  .questions, .missing { display: grid; gap: 8px; }
  .questions > li, .missing > li { overflow-wrap: anywhere; }
  .missing > li { color: var(--muted); }
  .options { margin: 2px 0 2px 2px; list-style: disc; padding-left: 16px; color: var(--muted); }
  .legend { border-top: 1px solid var(--line); color: var(--muted); font-size: 13px; }
  .legend > summary { padding: 8px 14px; cursor: pointer; }
  .legend ul { list-style: none; margin: 0; padding: 0 14px 8px; display: flex; flex-wrap: wrap; gap: 6px 14px; }
  .legend li { display: inline-flex; align-items: center; gap: 5px; }
  .legend .sev, .legend .ans { margin-top: 0; }
  .legend p { margin: 0; padding: 0 14px 6px; }
  .legend p:last-child { padding-bottom: 10px; }
  .act { border-top: 1px solid var(--line); padding: 10px 14px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; background: var(--surface-2); }
  .ask { font-size: 13px; flex-basis: 100%; }
  .hint { font-size: 12px; }
</style>
