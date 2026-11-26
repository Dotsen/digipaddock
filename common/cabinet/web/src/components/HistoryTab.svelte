<script module lang="ts">
  /** Events one page of the history brings. */
  export const HISTORY_PAGE = 100;
</script>

<script lang="ts">
  // A card's history: the newest page first, older pages on request, the card's new events on
  // top as they come. An event a thread message stands for (an edit's comment) is shown once, as its write.
  import { onMount } from "svelte";
  import { apiGet } from "../lib/api";
  import { actorName, describeEvent, isEcho } from "../lib/events";
  import { errorText, t } from "../lib/i18n.svelte";
  import { onLiveEvent } from "../lib/live.svelte";
  import { synced } from "../lib/synced.svelte";
  import { when } from "../lib/time";
  import { toast } from "../lib/toast.svelte";
  import type { EventsPage, JournalEvent } from "../lib/types";

  let { cardId }: { cardId: string } = $props();

  let events = $state<JournalEvent[]>([]);
  let next = $state<number | null>(null);
  let loaded = $state(false);
  let loading = $state(false);
  let paging = $state(false);
  const busy = $derived(loading || paging);
  let through = 0; // newest server snapshot, independent of events delivered live
  let gone = false;

  const path = () => `/api/cards/${encodeURIComponent(cardId)}/events`;

  function merge(list: JournalEvent[], incoming: JournalEvent[]): JournalEvent[] {
    const bySeq = new Map(list.map((e) => [e.seq, e]));
    for (const event of incoming) if (!isEcho(event)) bySeq.set(event.seq, event);
    return [...bySeq.values()].sort((a, b) => b.seq - a.seq);
  }

  const data = synced(async () => {
    loading = true;
    const initial = !loaded;
    try {
      let page = await apiGet<EventsPage>(`${path()}?limit=${HISTORY_PAGE}`);
      const first = page;
      let incoming = [...page.events];
      // Reconnect may cover more than one page. Read back to the last snapshot before advancing it.
      while (!initial && page.next !== null && page.next > through) {
        const before = page.next;
        page = await apiGet<EventsPage>(`${path()}?before=${before}&limit=${HISTORY_PAGE}`);
        incoming.push(...page.events);
        if (page.next !== null && page.next >= before) break;
      }
      if (gone) return;
      events = merge(events, incoming);
      through = Math.max(through, ...first.events.map((e) => e.seq));
      if (initial) next = first.next;
      loaded = true;
    } finally {
      loading = false;
    }
  });

  async function older() {
    if (next === null || busy) return;
    paging = true;
    try {
      const page = await apiGet<EventsPage>(`${path()}?before=${next}&limit=${HISTORY_PAGE}`);
      events = merge(events, page.events);
      next = page.next;
    } catch (exc) {
      toast(errorText(exc), "error");
    } finally {
      paging = false;
    }
  }

  onMount(() => {
    void data.run();
    const stop = onLiveEvent((event) => {
      if (event.card === cardId) events = merge(events, [event]); // one before the first page is kept too
    });
    return () => { gone = true; stop(); data.dispose(); };
  });
</script>

{#if data.error && !loaded}
  <p class="empty" role="alert">
    {t("history.failed")}
    <button type="button" class="btn small ghost" onclick={() => void data.run()}>{t("app.retry")}</button>
  </p>
{:else if !loaded}
  <p class="empty">{t("app.loading")}</p>
{:else if events.length === 0 && next === null}
  <p class="empty">{t("history.empty")}</p>
{:else}
  <ol class="history">
    {#each events as event (event.seq)}
      <li data-seq={event.seq}>
        <span class="muted">{when(event.ts ?? "")}</span>
        <b>{actorName(event.actor)}</b>
        <span>{describeEvent(event)}</span>
      </li>
    {/each}
  </ol>
  {#if next !== null}
    <button type="button" class="btn small ghost more" disabled={busy} onclick={() => void older()}>{t("thread.older")}</button>
  {/if}
{/if}

<style>
  .history { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--sp-8); font-size: var(--fs-ui); }
  .history li { display: flex; gap: var(--sp-8); flex-wrap: wrap; }
  .history b { font-weight: 600; }
  .more { margin-top: var(--sp-12); }
</style>
