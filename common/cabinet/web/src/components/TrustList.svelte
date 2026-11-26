<script lang="ts">
  // The monitor's trust on a P-n or a server page: each monitor_trust decision — what becomes trusted,
  // its state and how the monitor applied it, a link to the decision — and the addresses taken off the list.
  import { t, type Key } from "../lib/i18n.svelte";
  import { applyLine, removalLine, trustTitle } from "../lib/trust";
  import { age, when } from "../lib/time";
  import type { TrustDecision, TrustRemoval } from "../lib/types";

  let { decisions, removals = [] }: { decisions: TrustDecision[]; removals?: TrustRemoval[] } = $props();

  const who = (actor: string) => actor.replace(/^(user|agent):/, "@");
  // never a top-level `state`: Svelte would read `$state` as a store of it
  const stateLine = (d: TrustDecision) =>
    d.state === "approved" && d.apply_state ? applyLine(d.apply_state, d.apply_detail, d.applied_at)
      : t(`decision.state.${d.state}` as Key);
</script>

{#if decisions.length || removals.length}
  <ul class="rows">
    {#each [...decisions].reverse() as d (d.id)}
      <li class={d.state === "approved" ? d.apply_state : d.state}>
        <a href="/decisions/{d.id}">{trustTitle(d.payload)}</a>
        <span class="muted">{t("trust.proposed_by", { who: who(d.payload.proposed_by || d.requested_by) })}</span>
        <span class="muted nw" title={when(d.created)}>{age(d.created)}</span>
        <span class="state">{stateLine(d)}</span>
      </li>
    {/each}
  </ul>
  {#if removals.length}
    <h3>{t("trust.removals")}</h3>
    <ul class="rows">
      {#each removals as r (r.id)}
        <li class={r.state}>
          <code>{r.item}</code>
          <span class="muted">{who(r.requested_by)}</span>
          <span class="muted nw" title={when(r.created)}>{age(r.created)}</span>
          <span class="state">{removalLine(r)}</span>
        </li>
      {/each}
    </ul>
  {/if}
{:else}<p class="empty">{t("trust.empty")}</p>{/if}

<style>
  .rows { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--sp-2); }
  .rows li { display: flex; flex-wrap: wrap; gap: var(--sp-4) var(--sp-10); align-items: baseline; padding: var(--sp-6) 0; border-bottom: 1px solid var(--line-soft); font-size: var(--fs-md); }
  .rows a { color: var(--accent); text-decoration: none; }
  .nw { white-space: nowrap; }
  .state { color: var(--muted); }
  .applied .state { color: var(--good); }
  .error .state, .rejected .state { color: var(--crit); }
  .open .state, .pending .state, .queued .state { color: var(--warn); }
  h3 { font-size: var(--fs-md); margin: var(--sp-10) 0 var(--sp-4); }
  .empty { margin: 0; padding: var(--sp-8) 0; font-size: var(--fs-md); }
</style>
