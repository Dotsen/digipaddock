<script lang="ts">
  // How fresh a screen's data is: when it was last read, a refresh that failed over the data
  // shown, or — when the first read failed — the error with «retry» in place of an endless «loading».
  import { errorText, t } from "../lib/i18n.svelte";
  import type { Synced } from "../lib/synced.svelte";
  import { clock } from "../lib/time";

  let { data }: { data: Synced } = $props();
</script>

{#if data.updated === 0 && data.error}
  <p class="fresh failed" role="alert">
    <span>{errorText(data.error)}</span>
    <button class="link" type="button" onclick={() => void data.run()}>{t("app.retry")}</button>
  </p>
{:else if data.updated && data.stale}
  <p class="fresh stale" role="status">
    <span>{t("fresh.failed")} ·</span>
    <button class="link" type="button" onclick={() => void data.run()}>{t("app.retry")}</button>
  </p>
{:else if data.updated}
  <p class="fresh muted">{t("fresh.updated", { time: clock(data.updated) })}</p>
{/if}

<style>
  .fresh { display: flex; flex-wrap: wrap; gap: 4px 6px; align-items: baseline; margin: 0; font-size: 12px; }
  .failed { color: var(--crit); }
  .stale { color: var(--warn); }
  .link { border: 0; padding: 0; background: none; color: var(--accent); font: inherit; cursor: pointer; }
</style>
