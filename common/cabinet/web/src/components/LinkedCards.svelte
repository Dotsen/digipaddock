<script lang="ts">
  import { t } from "../lib/i18n.svelte";
  import { activeCards } from "../lib/monitoring";
  import { router, navigate } from "../lib/router.svelte";
  import type { LinkedCard } from "../lib/types";
  let { cards }: { cards: LinkedCard[] } = $props();
  const filter = $derived(router.current.query.get("cards") === "all" ? "all" : "active");
  const visible = $derived(filter === "active" ? activeCards(cards) : cards);
  function select(value: string) {
    const q = new URLSearchParams(router.current.query);
    if (value === "all") q.set("cards", "all"); else q.delete("cards");
    navigate(`${router.current.path.split("?")[0]}${q.size ? `?${q}` : ""}`);
  }
</script>

<div class="monitor-switch" role="group" aria-label={t("server.cards")}>
  <button class="btn" class:chosen={filter === "active"} aria-pressed={filter === "active"} onclick={() => select("active")}>{t("mon.active_cards")} ({activeCards(cards).length})</button>
  <button class="btn" class:chosen={filter === "all"} aria-pressed={filter === "all"} onclick={() => select("all")}>{t("mon.all_cards")} ({cards.length})</button>
</div>
<ul class="monitor-rows">
  {#each visible as c (c.id)}
    <li class="monitor-row">
      <a href="/cards/{c.id}"><b>{c.id}</b> · {c.title}</a>
      <div class="monitor-meta"><span>{c.status}</span><span>{c.assignee}</span></div>
      {#if c.pending_decision}<p>{t("mon.waiting_decision")}</p>{/if}
      {#if c.summary}<p class="monitor-note">{c.summary.slice(0, 700)}</p>{/if}
    </li>
  {:else}<li class="monitor-empty">{t("mon.no_cards")}</li>{/each}
</ul>
