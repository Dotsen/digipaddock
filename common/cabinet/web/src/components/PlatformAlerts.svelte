<script lang="ts">
  import { onMount } from "svelte";
  import { apiGet } from "../lib/api";
  import { errorText, hasKey, t, type Key } from "../lib/i18n.svelte";
  import { onLiveEvent } from "../lib/live.svelte";
  import { levelText } from "../lib/monitoring";
  import { session } from "../lib/session.svelte";
  import { when } from "../lib/time";
  import type { PlatformAlert, PlatformAlertPage } from "../lib/types";
  // The platform's own alerts — notices without a card: a failed unit, a release, a factory operation. A push about
  // one leads to the incidents page, and this is where it is read: the last week's, the older ones behind a button.
  // People with the settings see them (the feed's rule); nobody else is asked, and no alert at all is no block.
  const RECENT_MS = 7 * 24 * 3600 * 1000;
  const PAGE = 5;
  let { now = Date.now() }: { now?: number } = $props();
  let items = $state<PlatformAlert[]>([]);
  let next = $state<number | null>(null);
  let older = $state(false); // the reader asked for the alerts before the last week
  let busy = $state(false);
  let error = $state<unknown>(null);
  let generation = 0;
  const allowed = $derived(session.can("settings"));
  const shown = $derived(older ? items : items.filter((a) => now - Date.parse(a.ts) <= RECENT_MS));
  const more = $derived(shown.length < items.length || next !== null);
  // the incident levels' words and colours: a critical notice reads as a critical incident does
  const level = (a: PlatformAlert) => (a.level === "critical" ? "crit" : "warn");
  const text = (a: PlatformAlert) => (hasKey(a.key) ? t(a.key as Key, a.params) : t("feed.notice"));

  async function load() {
    const current = ++generation;
    try {
      const page = await apiGet<PlatformAlertPage>(`/api/alerts?limit=${PAGE}`);
      if (current !== generation) return;
      // a new alert must not drop the older pages the reader opened: what lies before this page stays —
      // while the page reaches what was loaded. A burst that ends above it starts the list again, or the
      // alerts in between would fall into a gap no button fetches
      const oldest = page.items[page.items.length - 1]?.seq ?? 0;
      const reaches = items.length > 0 && oldest <= items[0].seq;
      const kept = reaches ? items.filter((a) => a.seq < oldest) : [];
      items = [...page.items, ...kept];
      if (!kept.length) next = page.next;
      error = null;
    } catch (failure) {
      if (current === generation) error = failure;
    }
  }
  async function showOlder() {
    const hidden = items.length - shown.length;
    older = true;
    if (hidden || next === null) return; // the loaded ones first
    busy = true;
    const current = generation;
    try {
      const page = await apiGet<PlatformAlertPage>(`/api/alerts?limit=${PAGE}&before=${next}`);
      if (current !== generation) return;
      items = [...items, ...page.items];
      next = page.next;
      error = null;
    } catch (failure) {
      error = failure;
    } finally {
      busy = false;
    }
  }
  onMount(() => {
    if (!allowed) return;
    void load();
    return onLiveEvent((e) => {
      if (e.kind === "notice" && !e.card) void load();
    });
  });
</script>

{#if allowed && (items.length || error)}
  <section class="platform-alerts" aria-labelledby="platform-alerts-title">
    <h2 id="platform-alerts-title">{t("mon.alerts.title")}</h2>
    <p class="monitor-note">{t("mon.alerts.hint")}</p>
    {#if error}<p class="error-text" role="alert">{errorText(error)}</p>{/if}
    {#if shown.length}
      <ul class="monitor-rows">
        {#each shown as a (a.seq)}
          <li class="monitor-row platform-alert" class:critical={a.level === "critical"}>
            <div class="monitor-row-head">
              <span class="chip monitor-badge {level(a)}">{levelText(level(a))}</span>
              <time class="monitor-note" datetime={a.ts}>{when(a.ts)}</time>
            </div>
            <p>{text(a)}</p>
          </li>
        {/each}
      </ul>
    {:else if items.length}<p class="monitor-note">{t("mon.alerts.none_recent")}</p>
    {/if}
    {#if more}<button class="btn" disabled={busy} onclick={showOlder}>{t("mon.alerts.older")}</button>{/if}
  </section>
{/if}

<style>
  .platform-alerts { display: grid; gap: var(--sp-8); margin: 0 0 18px; }
  .platform-alerts h2 { margin: 0; font-size: var(--fs-lead); }
  .platform-alerts .monitor-note { margin: 0; }
  .platform-alerts .btn { justify-self: start; }
  .platform-alert.critical { border-color: var(--crit); border-top-width: 2px; }
</style>
