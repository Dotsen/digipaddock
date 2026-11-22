<script module lang="ts">
  import type { Key } from "../lib/i18n.svelte";

  export function riskKey(level: string): Key {
    return level === "high" ? "risk.high" : level === "medium" ? "risk.medium" : "risk.low";
  }
</script>

<script lang="ts">
  // A sysadmin plan's steps: what (first line), risk by colour, the server (production highlighted), the
  // exact command folded under ▸, the rollback. A script step shows its text unfolded: the owner's
  // signature covers exactly that text, so it is read, not opened.
  import { t } from "../lib/i18n.svelte";
  import type { PlanStep } from "../lib/types";

  let { steps }: { steps: PlanStep[] } = $props();
</script>

<ol class="steps">
  {#each steps as step (step.n)}
    <li class="step" class:prod={step.production}>
      <div class="st">
        <span class="n">{step.n}.</span>
        <span>{step.what}</span>
        <span class="risk {step.risk.level}">{t(riskKey(step.risk.level))}</span>
      </div>
      <div class="srv">
        {#each step.servers as server (server)}<span class="chip">{server}</span>{/each}
        {#if step.production}<span class="chip crit">{t("decision.production")}</span>{/if}
      </div>
      {#if step.effect}<div class="x"><b>{t("decision.effect")}:</b> {step.effect}</div>{/if}
      {#if step.risk.why}<div class="x"><b>{t("decision.risk")}:</b> {step.risk.why}</div>{/if}
      {#if step.rollback}<div class="x"><b>{t("decision.rollback")}:</b> {step.rollback}</div>{/if}
      {#if step.kind === "script"}
        <div class="cmd">
          <span class="label">{t("decision.script")}</span>
          <pre class="script">{step.command}</pre>
        </div>
      {:else}
        <details class="cmd">
          <summary>{t("decision.command")}</summary>
          <pre>{step.command || step.mechanics}</pre>
        </details>
      {/if}
    </li>
  {/each}
</ol>

<style>
  .steps { list-style: none; margin: 0; padding: 0; }
  .step { border-top: 1px solid var(--line); padding: 10px 14px; display: grid; gap: 4px; }
  .step.prod { background: var(--crit-bg); }
  .st { display: flex; gap: 8px; align-items: baseline; font-weight: 600; }
  .n { color: var(--muted); font-weight: 500; font-variant-numeric: tabular-nums; }
  .risk { margin-left: auto; font-size: 12px; font-weight: 500; white-space: nowrap; }
  .risk.low { color: var(--good); } .risk.medium { color: var(--warn); } .risk.high { color: var(--crit); }
  .srv { display: flex; gap: 6px; flex-wrap: wrap; }
  .x { color: var(--muted); font-size: 13px; max-width: 62ch; }
  .x b { color: var(--text); font-weight: 500; }
  .cmd { font-family: var(--font-mono); font-size: 12.5px; color: var(--muted); }
  .cmd summary { cursor: pointer; font-family: var(--font-ui); font-size: 12px; color: var(--accent); }
  .cmd .label { font-family: var(--font-ui); font-size: 12px; color: var(--warn); }
  .cmd pre { margin: 6px 0 0; padding: 8px 10px; background: var(--bg); border-radius: 6px; overflow-x: auto; color: var(--text); white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
