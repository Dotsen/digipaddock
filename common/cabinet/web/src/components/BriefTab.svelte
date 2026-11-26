<script lang="ts">
  // The card's brief tab (R17, R18): the brief's state in words, what changed since its last version when the
  // text moved on, the description with the AC and DoD checklists (the editors), and the versions newest first —
  // each with its author, date, origin, approval and what it changed. A card that never had a brief shows its
  // original statement, not an empty screen.
  import { parseTaskLines, type Segment } from "../lib/checklist";
  import { runText, stateText, writerOf } from "../lib/council";
  import { actorName } from "../lib/events";
  import { t, type Key } from "../lib/i18n.svelte";
  import { reasonText } from "../lib/runReason";
  import { checklistCounts } from "../lib/stores/card.svelte";
  import { briefLabel, briefTone } from "../lib/stores/brief.svelte";
  import { models } from "../lib/stores/models.svelte";
  import { when } from "../lib/time";
  import type { BriefVersion, BriefView, Card, DiffLine } from "../lib/types";
  import Checklist from "./Checklist.svelte";
  import Editor from "./Editor.svelte";
  import FactorySteps from "./FactorySteps.svelte";
  import StagePlan from "./StagePlan.svelte";
  import Markdown from "./Markdown.svelte";

  let {
    view,
    card,
    canEdit,
    busy,
    draftKey,
    onsave,
    ontoggleac,
    onaddac,
    ontoggledod,
    onadddod,
  }: {
    view: BriefView | null;
    card: Card;
    canEdit: boolean;
    busy: boolean;
    draftKey: string;
    onsave: (text: string, revision: number) => Promise<boolean>;
    ontoggleac: (line: number) => void;
    onaddac: (text: string) => Promise<boolean>;
    ontoggledod: (line: number) => void;
    onadddod: (text: string) => Promise<boolean>;
  } = $props();

  const counts = $derived(checklistCounts(card));
  const acSegments = $derived<Segment[]>(
    card.acceptance.map((a, line) => ({ type: "item", line, checked: a.checked, text: a.text })),
  );
  const dodSegments = $derived(parseTaskLines(card.dod ?? ""));
  const newestFirst = $derived(view ? [...view.versions].reverse() : []);
  const KIND = { "=": "same", "-": "del", "+": "add", "…": "skip" } as const;
  // the council: how the card's last one stands, and per version the round that read it
  const council = $derived(view?.brief.council ?? null);
  const readBy = (read: NonNullable<BriefVersion["council"]>) =>
    read.runs.map((run) => `${models.name(run.model)} — ${runText(run)}`).join(", ");
</script>

{#snippet diff(lines: DiffLine[])}
  <ol class="diff">
    {#each lines as line, i (i)}
      <li class="d-{KIND[line.op]}">
        {#if line.op === "…"}{t("brief.unchanged", { n: line.text })}{:else}{line.text || " "}{/if}
      </li>
    {/each}
  </ol>
{/snippet}

<div class="pane stack">
  {#if view}
    <p class="state">
      <span class="chip {briefTone(view.brief)}">{briefLabel(view.brief)}</span>
      {#if view.brief.state === "failed" && view.brief.error}<span class="muted" title={view.brief.error}>{reasonText(view.brief.error)}</span>{/if}
      {#if council}
        <span class="muted">{t("brief.council", { round: council.round, rounds: council.rounds, state: stateText(council, writerOf(council.kind, card.assignee)) })}</span>
      {/if}
    </p>
    {#if view.brief.state === "not_requested"}<p class="muted">{t("brief.none")}</p>{/if}
    {#if view.changes.length}
      <h2 class="section-h">{t("brief.changes", { version: view.brief.version })}</h2>
      {@render diff(view.changes)}
    {/if}
  {/if}
  <Editor value={card.description} revision={card.revision} {draftKey} editable={canEdit} {onsave} />
  <h2 class="section-h">{t("card.acceptance")} {#if counts.ac[1]}<span class="n">{counts.ac[0]}/{counts.ac[1]}</span>{/if}</h2>
  <Checklist segments={acSegments} editable={canEdit} {busy} label={t("card.acceptance")} ontoggle={ontoggleac} onadd={onaddac} />
  <h2 class="section-h">{t("card.dod")} {#if counts.dod[1]}<span class="n">{counts.dod[0]}/{counts.dod[1]}</span>{/if}</h2>
  <Checklist segments={dodSegments} editable={canEdit} {busy} label={t("card.dod")} ontoggle={ontoggledod} onadd={onadddod} />
  {#if view?.current.operations?.length}
    <section class="ops-now">
      <h2 class="section-h">{t("brief.operations")}</h2>
      <FactorySteps steps={view.current.operations} />
    </section>
  {/if}
  {#if view?.current.stages?.length}
    <section class="stages-now">
      <h2 class="section-h">{t("brief.stages")}</h2>
      <StagePlan stages={view.current.stages} />
    </section>
  {/if}
  <p class="muted rev">{t("card.revision", { revision: card.revision })}</p>
  {#if newestFirst.length}
    <h2 class="section-h">{t("brief.versions")}</h2>
    <ol class="versions">
      {#each newestFirst as version (version.version)}
        <li>
          <details>
            <summary>
              <b>{t("brief.meta", { version: version.version, author: actorName(version.author), date: when(version.created) })}</b>
              <span class="muted">{t(`brief.origin.${version.origin}` as Key)}</span>
              {#if version.approved_by}
                <span class="chip good">{t("brief.approved_by", { who: actorName(version.approved_by), date: when(version.approved_at) })}</span>
              {/if}
              {#if version.council}
                <span class="muted read">{t("brief.council_round", { round: version.council.round })}: {readBy(version.council)}</span>
              {/if}
            </summary>
            {#if version.changes.length}
              {@render diff(version.changes)}
            {:else}
              <div class="text">
                <Markdown text={version.description} />
                {#if version.acceptance.length}
                  <ul>{#each version.acceptance as item, i (i)}<li>{item}</li>{/each}</ul>
                {/if}
              </div>
            {/if}
            {#if version.operations?.length}<FactorySteps steps={version.operations} />{/if}
            {#if version.stages?.length}<StagePlan stages={version.stages} compact />{/if}
          </details>
        </li>
      {/each}
    </ol>
  {/if}
</div>

<style>
  .pane { flex: 1; min-height: 0; overflow-y: auto; padding: var(--sp-14) var(--sp-2); }
  .pane .section-h { margin: var(--sp-8) 0 0; }
  .state { margin: 0; display: flex; gap: var(--sp-8); align-items: center; flex-wrap: wrap; }
  .rev { font-size: var(--fs-sm); margin: 0; }
  .diff { list-style: none; margin: 0; padding: var(--sp-8) var(--sp-10); border: 1px solid var(--line); border-radius: var(--r-md);
    font: 13px/1.5 var(--mono, ui-monospace, monospace); overflow-x: auto; }
  .diff li { white-space: pre-wrap; overflow-wrap: anywhere; }
  .diff .d-del { color: var(--crit); text-decoration: line-through; }
  .diff .d-add { color: var(--good); }
  .diff .d-del::before { content: "− "; } .diff .d-add::before { content: "+ "; } .diff .d-same::before { content: "  "; }
  .diff .d-skip { color: var(--muted); font-style: italic; }
  .versions { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--sp-8); }
  .versions summary { cursor: pointer; display: flex; gap: var(--sp-8); flex-wrap: wrap; align-items: center; }
  .versions details > :not(summary) { margin-top: var(--sp-8); }
  .text ul { margin: var(--sp-6) 0 0; padding-left: var(--sp-20); }

</style>
