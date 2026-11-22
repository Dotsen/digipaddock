<script lang="ts">
  // The dev.check contours under the servers: whether each takes checks, how many at once, what runs there
  // now — the card it is for — and, in maintenance, why. Nothing at all when the host has no contours.
  import { t } from "../lib/i18n.svelte";
  import { when } from "../lib/time";
  import type { ContourRow } from "../lib/types";

  let { rows }: { rows: ContourRow[] } = $props();
  const CLS = { idle: "ok", busy: "warn", maintenance: "crit" } as const;
</script>

{#if rows.length}
  <section class="contours">
    <h2>{t("contours.title")}</h2>
    <div class="list">
      {#each rows as row (row.name)}
        <div class="item">
          <span class="chip text level {CLS[row.state]}">{t(`contours.state.${row.state}`)}</span>
          <span class="body">
            <b>{row.name}</b>
            <span class="muted line">{t("contours.capacity", { n: row.capacity })} · {row.components.join(", ")}</span>
            {#if row.state === "maintenance" && row.reason}
              <span class="line">{row.reason}{#if row.since} · {t("contours.since", { at: when(row.since) })}{/if}</span>
            {/if}
            {#each row.running as job (job.operation)}
              <a class="line" href="/cards/{job.card}">{job.card} · {job.project}#{job.pr} · {job.head.slice(0, 7)}</a>
            {/each}
          </span>
        </div>
      {/each}
    </div>
  </section>
{/if}

<style>
  .contours { margin-top: 24px; }
  h2 { font-size: 15px; margin: 0 0 8px; }
  .list { display: flex; flex-direction: column; gap: 8px; }
  .item {
    display: grid; grid-template-columns: minmax(0, 128px) minmax(0, 1fr); gap: 10px; align-items: center; padding: 12px;
    background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); color: var(--text);
  }
  .body { display: grid; gap: 2px; min-width: 0; }
  .line { font-size: 12px; overflow-wrap: anywhere; }
  .level { justify-content: center; text-align: center; min-width: 0; }
  .level.crit { color: var(--crit); background: var(--crit-bg); }
  .level.warn { color: var(--warn); background: var(--warn-bg); }
  .level.ok { color: var(--good); background: var(--good-bg); }
  @media (max-width: 599px) {
    .item { grid-template-columns: minmax(0, 1fr); align-items: start; }
    .level { justify-self: start; }
  }
</style>
