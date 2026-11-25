<script lang="ts">
  // A card in a list (board column, review group): id, title, assignee, project (its chip in the project's colour),
  // labels, servers; a card put off until a time says until when, in the person's time zone, and is shaded as a whole —
  // not in work, still readable.
  import { t } from "../lib/i18n.svelte";
  import { session } from "../lib/session.svelte";
  import { dayTime } from "../lib/time";
  import type { CardCompact } from "../lib/types";
  import Avatar from "./Avatar.svelte";
  import ProjectChip from "./ProjectChip.svelte";
  import StatusPill from "./StatusPill.svelte";

  // status: the card's pill; "phone" — below 900px only (the board's columns name the status on a desktop)
  let { card, status = false }: { card: CardCompact; status?: boolean | "phone" } = $props();
</script>

<a class="bc" class:deferred={!!card.defer} href="/cards/{card.id}">
  <span class="t">{card.title}</span>
  <span class="row">
    <span class="id">{card.id}</span>
    {#if status}<span class="pill" class:phone={status === "phone"}><StatusPill status={card.status} /></span>{/if}
    {#if card.defer}<span class="chip defer" title={card.defer.reason}>{t("defer.badge", { when: dayTime(card.defer.until, session.me?.timezone) })}</span>{/if}
    {#if card.assignee}<Avatar name={card.assignee} kind={card.assignee === "@boss" ? "boss" : card.assignee.startsWith("@") ? "agent" : "owner"} size={20} />{/if}
    <ProjectChip id={card.project} />
    {#each card.servers as server (server)}<span class="chip text">{server}</span>{/each}
    {#each card.labels as label (label)}<span class="chip acc text">{label}</span>{/each}
  </span>
</a>

<style>
  .bc { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 10px 11px; display: grid; gap: 6px; text-decoration: none; color: var(--text); min-width: 0; }
  .bc:hover { border-color: var(--muted); }
  .t { font-size: 14px; font-weight: 500; line-height: 1.3; overflow-wrap: anywhere; min-width: 0; }
  .id { color: var(--muted); font-size: 12px; }
  .row { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; min-width: 0; }
  .pill { display: inline-flex; }
  .defer { color: var(--muted); white-space: nowrap; }
  /* put off until a time: sunk into the page — hatched, a dashed edge, the title muted; the muted text keeps 4.5:1 at
     this opacity */
  .bc.deferred {
    background: repeating-linear-gradient(-45deg, transparent 0 6px, var(--line-soft) 6px 8px), var(--bg);
    border-style: dashed; opacity: .8;
  }
  .bc.deferred .t { color: var(--muted); }
  .bc.deferred:hover, .bc.deferred:focus-visible { opacity: 1; }
  @media (min-width: 900px) { .pill.phone { display: none; } }
</style>
