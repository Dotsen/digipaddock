<script module lang="ts">
  import { hasKey, t } from "../lib/i18n.svelte";

  /** Why a stage is held for the owner: the cabinet's own reasons by their code, BOSS's as it wrote them. */
  function holdReason(reason: string): string {
    const [code, ...rest] = reason.split(": ");
    const key = `stage.reason.${code}`;
    return hasKey(key) ? t(key, { why: rest.join(": ") }) : reason;
  }

  /** Where a stage of a chain plan stands at its gate; "" for a stage with nothing to say yet. */
  export function gateText(gate?: string, reason = ""): string {
    const key = `stage.gate.${gate}`;
    return gate && hasKey(key) ? t(key, { reason: holdReason(reason) }) : "";
  }
</script>

<script lang="ts">
  // The stages of a brief: the parts a large task splits into, each with its role and specialization,
  // what it waits for and — once the owner approved the brief — the child card it became and that card's status.
  // Shown on the brief tab, before a start approves the brief, and on the aggregate's next step (compact).
  // A chain plan says so, and each of its stages says where it stands between its hand-in and the next stage.
  import type { Stage } from "../lib/types";
  import { statusKey, statusTone } from "./StatusPill.svelte";

  let { stages, compact = false, chain = false }: { stages: Stage[]; compact?: boolean; chain?: boolean } = $props();
</script>

{#if chain}<p class="chain">{t("stage.chain")}</p>{/if}
<ol class="stages" class:compact>
  {#each stages as stage (stage.key)}
    <li>
      <div class="st">
        <span class="key">{stage.key}</span>
        <b>{stage.title}</b>
        {#if stage.child}
          <a class="chip" href="/cards/{encodeURIComponent(stage.child)}">{stage.child}</a>
          {#if stage.status}<span class="pill {statusTone(stage.status)}">{t(statusKey(stage.status))}</span>{/if}
        {/if}
      </div>
      {#if gateText(stage.gate, stage.reason)}<div class="x gate">{gateText(stage.gate, stage.reason)}</div>{/if}
      <div class="x">
        {[
          `${t("stage.role")}: ${[stage.role, stage.spec].filter(Boolean).join(" · ")}`,
          stage.depends?.length ? t("stage.after", { keys: stage.depends.join(", ") }) : "",
        ].filter(Boolean).join(" · ")}
      </div>
      {#if !compact}
        {#if stage.description}<div class="x">{stage.description}</div>{/if}
        {#if stage.acceptance?.length}
          <ul class="ac">{#each stage.acceptance as item, i (i)}<li>{item}</li>{/each}</ul>
        {/if}
      {/if}
    </li>
  {/each}
</ol>

<style>
  .chain { margin: 0; font-size: var(--fs-md); color: var(--muted); }
  .gate { color: var(--text); }
  .stages { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--sp-8); }
  .stages li { border: 1px solid var(--line); border-radius: var(--r-md); padding: var(--sp-8) var(--sp-10); display: grid; gap: 3px; }
  .stages.compact li { padding: var(--sp-6) var(--sp-8); gap: var(--sp-2); }
  .st { display: flex; gap: var(--sp-8); align-items: baseline; flex-wrap: wrap; }
  .key { font-family: ui-monospace, monospace; font-size: var(--fs-sm); color: var(--muted); }
  .x { color: var(--muted); font-size: var(--fs-md); max-width: 62ch; overflow-wrap: anywhere; }
  .ac { margin: 0; padding-left: 18px; font-size: var(--fs-md); }
  .pill { font-size: var(--fs-sm); margin-left: auto; white-space: nowrap; }
</style>
