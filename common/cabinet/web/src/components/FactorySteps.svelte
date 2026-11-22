<script lang="ts">
  // A brief's "Operations": what approving it grants the factory — each step's what and risk, its kind,
  // target and roles, its effect, why the risk, its rollback. Shown on the brief tab and before a start approves it.
  // A component.install step names its component, mode and the deployed revision the signature covers;
  // one on revision "release" runs on the release of this card's own PR, whatever tree that brings.
  import { t } from "../lib/i18n.svelte";
  import type { FactoryStep } from "../lib/types";
  import { riskKey } from "./PlanSteps.svelte";

  let { steps }: { steps: FactoryStep[] } = $props();
</script>

<ol class="ops">
  {#each steps as op, i (i)}
    <li>
      <div class="st"><b>{op.what}</b> <span class="risk {op.risk?.level}">{t(riskKey(op.risk?.level ?? ""))}</span></div>
      {#if op.component}
        <div class="x">{op.verb} · {op.target} · {op.component} {op.mode} · {t(op.revision === "release" ? "decision.revision_release" : "decision.revision")}</div>
      {:else}
        <div class="x">{op.verb} · {op.target} · {(op.agents ?? []).join(", ")}</div>
      {/if}
      {#if op.effect}<div class="x"><b>{t("decision.effect")}:</b> {op.effect}</div>{/if}
      {#if op.risk?.why}<div class="x"><b>{t("decision.risk")}:</b> {op.risk.why}</div>{/if}
      {#if op.rollback}<div class="x"><b>{t("decision.rollback")}:</b> {op.rollback}</div>{/if}
    </li>
  {/each}
</ol>

<style>
  .ops { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
  .ops li { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; display: grid; gap: 3px; }
  .st { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
  .x { color: var(--muted); font-size: 13px; max-width: 62ch; overflow-wrap: anywhere; }
  .x b { color: var(--text); font-weight: 500; }
  .risk { margin-left: auto; font-size: 12px; white-space: nowrap; }
  .risk.low { color: var(--good); } .risk.medium { color: var(--warn); } .risk.high { color: var(--crit); }
</style>
