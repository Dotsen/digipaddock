<script lang="ts">
  // What the owner is asked to accept: the card's summary, the links in it and in the notes,
  // AC and DoD as they stand (read here, ticked in the brief), the plan and the notes, and every Review
  // iteration — who sent it with what, who accepted or returned it with what remarks. Read again whenever the
  // card changes; nothing here edits the card.
  import { onMount } from "svelte";
  import { apiGet } from "../lib/api";
  import { parseTaskLines, taskCounts } from "../lib/checklist";
  import { errorText, t, type Key } from "../lib/i18n.svelte";
  import { onLiveEvent } from "../lib/live.svelte";
  import { synced } from "../lib/synced.svelte";
  import { when } from "../lib/time";
  import type { CardResult, ReviewIteration } from "../lib/types";
  import Markdown from "./Markdown.svelte";

  let { cardId }: { cardId: string } = $props();

  let result = $state<CardResult | null>(null);
  const data = synced(async () => {
    const id = cardId;
    const found = await apiGet<CardResult>(`/api/cards/${encodeURIComponent(id)}/result`);
    if (id === cardId) result = found;
  });

  const ac = $derived(result ? [result.acceptance.filter((a) => a.checked).length, result.acceptance.length] : [0, 0]);
  const dod = $derived(result ? taskCounts(result.dod) : [0, 0]);
  const dodItems = $derived(result ? parseTaskLines(result.dod) : []);

  function decided(r: ReviewIteration): string {
    if (r.outcome === "pending") return t("result.pending");
    return t(`result.${r.outcome}`, { who: r.decided_by_name, when: when(r.decided) });
  }

  onMount(() => {
    void data.run();
    const stop = onLiveEvent((event) => {
      if (event.card === cardId && event.kind === "card.changed") void data.run();
    });
    return () => {
      stop();
      data.dispose();
    };
  });
</script>

<div class="result">
  {#if !result && data.error}
    <p class="empty" role="alert">
      {t("result.failed")}
      <button type="button" class="btn small ghost" onclick={() => void data.run()}>{t("app.retry")}</button>
    </p>
  {:else if !result}
    <p class="empty">{t("app.loading")}</p>
  {:else}
    <section>
      <h2>{t("result.summary")}</h2>
      {#if result.summary}<Markdown text={result.summary} />{:else}<p class="muted">{t("result.no_summary")}</p>{/if}
    </section>
    {#if result.links.length}
      <section>
        <h2>{t("result.links")}</h2>
        <ul class="links">
          {#each result.links as link (link)}<li><a href={link} target="_blank" rel="noopener noreferrer">{link}</a></li>{/each}
        </ul>
      </section>
    {/if}
    {#if result.acceptance.length}
      <section>
        <h2>{t("card.acceptance")} <span class="muted count">{t("result.done_of", { done: ac[0], total: ac[1] })}</span></h2>
        <ul class="checks">
          {#each result.acceptance as item, i (i)}
            <li class:done={item.checked}><span aria-hidden="true">{item.checked ? "✓" : "○"}</span> {item.text}</li>
          {/each}
        </ul>
      </section>
    {/if}
    {#if dod[1]}
      <section>
        <h2>{t("card.dod")} <span class="muted count">{t("result.done_of", { done: dod[0], total: dod[1] })}</span></h2>
        <ul class="checks">
          {#each dodItems as item, i (i)}
            {#if item.type === "item"}
              <li class:done={item.checked}><span aria-hidden="true">{item.checked ? "✓" : "○"}</span> {item.text}</li>
            {/if}
          {/each}
        </ul>
      </section>
    {/if}
    {#if result.release && result.release.state !== "none"}
      <section class="release {result.release.state}">
        <h2>{t("result.release")}</h2>
        <div class="state">{t(`result.release.${result.release.state}`, { rev: result.release.rev, host: result.release.host, when: when(result.release.at) })}</div>
        {#if result.release.why}<div class="note">{result.release.why}</div>{/if}
        {#if result.release.operations.length}
          <ul class="ops">
            {#each result.release.operations as op (op.id)}
              <li class={op.state}><span class="muted">{op.component || op.agents.join(", ")}{op.mode ? ` · ${op.mode}` : ""}</span> {t(`result.release.op.${op.state}` as Key)}{op.error ? ` — ${op.error}` : ""}</li>
            {/each}
          </ul>
        {/if}
      </section>
    {/if}
    {#if result.reviews.length}
      <section>
        <h2>{t("result.reviews")}</h2>
        <ol class="reviews">
          {#each result.reviews as r (r.iteration)}
            <li class={r.outcome}>
              <b>{t("result.iteration", { n: r.iteration })}</b>
              <span class="muted">{t("result.sent", { who: r.by_name, when: when(r.entered) })}</span>
              {#if r.entered_note}<div class="note">{r.entered_note}</div>{/if}
              <span class="outcome">{decided(r)}</span>
              {#if r.note}<div class="note">{r.note}</div>{/if}
            </li>
          {/each}
        </ol>
      </section>
    {/if}
    {#if result.plan}
      <details>
        <summary>{t("result.plan")}</summary>
        <Markdown text={result.plan} />
      </details>
    {/if}
    {#if result.notes}
      <details>
        <summary>{t("result.notes")}</summary>
        <Markdown text={result.notes} />
      </details>
    {/if}
    {#if data.stale}<p class="muted" role="status">{errorText(data.error)}</p>{/if}
  {/if}
</div>

<style>
  .result { display: grid; gap: var(--sp-16); }
  h2 { font-size: var(--fs-md); text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 0 0 var(--sp-8); font-weight: 600; }
  h2 .count { text-transform: none; letter-spacing: 0; font-weight: 400; margin-left: var(--sp-6); }
  .links, .checks, .reviews { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--sp-6); }
  .links a { color: var(--accent); overflow-wrap: anywhere; }
  .checks li { overflow-wrap: anywhere; }
  .checks li.done span { color: var(--good); }
  .reviews li { display: grid; gap: var(--sp-4); padding: var(--sp-10) var(--sp-12); border: 1px solid var(--line); border-radius: var(--r-md); background: var(--surface); }
  .reviews li.returned .outcome { color: var(--warn, var(--crit)); }
  .reviews li.accepted .outcome { color: var(--good); }
  .release .state { font-weight: 600; }
  .release.degraded .state, .release.refused .state { color: var(--crit); }
  .release.healthy .state { color: var(--good); }
  .release .ops { list-style: none; margin: var(--sp-6) 0 0; padding: 0; display: grid; gap: var(--sp-4); font-size: var(--fs-md); }
  .release .ops li.failed { color: var(--crit); }
  .note { white-space: pre-wrap; overflow-wrap: anywhere; font-size: var(--fs-ui); }
  details summary { cursor: pointer; font-weight: 600; }
</style>
