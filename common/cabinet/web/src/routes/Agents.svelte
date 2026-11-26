<script lang="ts">
  // The agents: each one's state from facts and every fact that is not zero, the questions
  // waiting for the person, its cards by status, its specializations, today's spend and, on the session host,
  // its slots. Live events refresh the list.
  import { onMount } from "svelte";
  import "../agent-state.css";
  import AgentFacts from "../components/AgentFacts.svelte";
  import Avatar from "../components/Avatar.svelte";
  import Badge from "../components/Badge.svelte";
  import { chats } from "../lib/chats.svelte";
  import Freshness from "../components/Freshness.svelte";
  import { apiGet } from "../lib/api";
  import { isToolMessage } from "../lib/events";
  import { coalesce } from "../lib/coalesce";
  import { errorText, t } from "../lib/i18n.svelte";
  import { onLiveEvent, watchAll } from "../lib/live.svelte";
  import { synced } from "../lib/synced.svelte";
  import { toast } from "../lib/toast.svelte";
  import type { AgentSummary } from "../lib/types";

  let agents = $state<AgentSummary[] | null>(null);

  const data = synced(
    async () => {
      agents = await apiGet<AgentSummary[]>("/api/agents");
    },
    { onError: (exc) => toast(errorText(exc), "error") },
  );
  const load = () => data.run();
  const refresh = coalesce(load);

  onMount(() => {
    void load();
    const release = watchAll();
    const stop = onLiveEvent((event) => {
      if (!isToolMessage(event)) refresh(); // a turn's tool calls change no fact here
    });
    const tick = setInterval(load, 60_000); // a heartbeat goes stale without any event
    return () => {
      refresh.cancel();
      clearInterval(tick);
      stop();
      release();
      data.dispose();
    };
  });

  const cost = (value: number) => `$${value.toFixed(2)}`;
</script>

<div class="topbar"><h1>{t("nav.agents")}</h1><Freshness {data} /></div>

{#if agents === null}
  {#if !data.error}<p class="empty">{t("app.loading")}</p>{/if}
{:else if agents.length === 0}
  <p class="empty">{t("agents.empty")}</p>
{:else}
  <div class="grid">
    {#each agents as agent (agent.name)}
      <article class="agent agent-state {agent.state}">
        <div class="head">
          <Avatar name={agent.name} kind={agent.name === "boss" ? "boss" : "agent"} size={34} />
          <a class="who" href="/agents/{agent.name}">@{agent.name}</a>
          <Badge count={chats.total(agent.name)} label={t("nav.unread", { name: `@${agent.name}`, count: chats.total(agent.name) })} />
          <span class="chip state"><span class="dot" aria-hidden="true"></span>{t(`agents.state.${agent.state}`)}</span>
        </div>
        <AgentFacts facts={agent.facts} cards={agent.cards} hosted={!!agent.host} />
        {#if agent.usage_today || agent.host}
          <p class="facts">
            {#if agent.usage_today}
              <span>{t("agents.today", { cost: cost(agent.usage_today.cost), calls: agent.usage_today.calls })}</span>
            {/if}
            {#if agent.host}<span>{t("agents.slots", { ...agent.host })}</span>{/if}
          </p>
        {/if}
        {#if agent.specs.length}
          <div class="specs">{#each agent.specs as spec (spec)}<span class="chip">{spec}</span>{/each}</div>
        {/if}
      </article>
    {/each}
  </div>
{/if}

<style>
  .grid { display: grid; gap: var(--sp-10); grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); }
  .agent {
    display: grid; gap: var(--sp-8); align-content: start; padding: var(--sp-14); border-radius: var(--radius); background: var(--surface);
    border: 2px var(--state-border-style) var(--state-color); min-width: 0;
  }
  .head { display: flex; flex-wrap: wrap; gap: var(--sp-10); align-items: center; }
  /* the name keeps its width: a state chip with no room left goes to the next line instead of covering it */
  .who { flex: 1 0 auto; max-width: 100%; overflow-wrap: anywhere; font-weight: 700; color: inherit; text-decoration: none; }
  .who:hover { color: var(--accent); }
  .facts { display: flex; flex-wrap: wrap; gap: var(--sp-4) var(--sp-12); font-size: var(--fs-md); margin: 0; }
  .specs { display: flex; flex-wrap: wrap; gap: var(--sp-6); align-items: center; }
</style>
