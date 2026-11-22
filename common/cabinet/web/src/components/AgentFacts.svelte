<script lang="ts">
  // What an agent's state is made of: every number that is not zero, the questions waiting
  // for the person (links to them), on the agent page its failures in words, its cards by status, and its
  // heartbeat and last action apart from progress. An agent in a digipaddock panel says what the cabinet cannot see.
  import { t } from "../lib/i18n.svelte";
  import { reasonText } from "../lib/runReason";
  import { age, when } from "../lib/time";
  import type { AgentCards, AgentFacts } from "../lib/types";
  import { statusKey } from "./StatusPill.svelte";

  let {
    facts,
    cards,
    hosted,
    failures = false,
  }: { facts: AgentFacts; cards: AgentCards; hosted: boolean; failures?: boolean } = $props();

  const NUMBERS = ["working", "waiting_you", "queued", "failed", "stale"] as const;
  const COLUMNS: [keyof AgentCards, string][] = [
    ["inbox", "Inbox"],
    ["to_do", "To Do"],
    ["in_progress", "In Progress"],
    ["review", "Review"],
    ["blocked", "Blocked"],
  ];
  const numbers = $derived(NUMBERS.filter((key) => facts[key] > 0));
  const columns = $derived(COLUMNS.filter(([key]) => cards[key] > 0));
</script>

{#if !hosted}
  <p class="limit muted">{t("agents.not_hosted")}</p>
{/if}
{#if numbers.length}
  <p class="line">{#each numbers as key (key)}<span>{t(`agents.fact.${key}`, { n: facts[key] })}</span>{/each}</p>
{/if}
{#each facts.questions as question (question.decision)}
  <p class="item">
    <span class="tag">{t("agents.question")}</span>
    <a href="/cards/{question.card}?decision={question.decision}">{question.card} · {question.title}</a>
  </p>
{/each}
{#if failures}
  {#each facts.failures as failure (failure.card + failure.at)}
    <p class="item">
      <span class="tag bad">{t("agents.failure")}</span>
      {#if failure.card}<a href="/cards/{failure.card}">{failure.card} · {failure.title}</a>{/if}
      <span title={failure.reason}>{reasonText(failure.reason)}</span>
      <span class="muted" title={when(failure.at)}>· {age(failure.at)}</span>
    </p>
  {/each}
{/if}
{#if columns.length}
  <p class="line">
    {#each columns as [key, status] (key)}<span>{t("agents.cards_in", { status: t(statusKey(status)), n: cards[key] })}</span>{/each}
  </p>
{/if}
{#if facts.heartbeat || facts.last_action || facts.progress}
  <p class="line muted">
    {#if facts.heartbeat}<span title={when(facts.heartbeat)}>{t("agents.heartbeat", { age: age(facts.heartbeat) })}</span>{/if}
    {#if facts.progress}<span title={when(facts.progress)}>{t("agents.progress_at", { age: age(facts.progress) })}</span>{/if}
  </p>
{/if}
{#if facts.last_action}
  <p class="item">
    <span class="muted" title={when(facts.last_action.at)}>{t("agents.last_action", { age: age(facts.last_action.at) })}:</span>
    {#if facts.last_action.card}<a href="/cards/{facts.last_action.card}">{facts.last_action.card}</a>{/if}
    <span class="text">{facts.last_action.text}</span>
  </p>
{/if}

<style>
  p { margin: 0; font-size: 13px; }
  .line { display: flex; flex-wrap: wrap; gap: 2px 12px; }
  .item { display: flex; flex-wrap: wrap; gap: 2px 6px; align-items: baseline; min-width: 0; }
  .item a { color: var(--accent); text-decoration: none; overflow-wrap: anywhere; }
  .tag { font-size: 12px; font-weight: 600; color: var(--accent); }
  .tag.bad { color: var(--crit); }
  .text { overflow-wrap: anywhere; min-width: 0; }
  .limit { font-size: 13px; }
</style>
