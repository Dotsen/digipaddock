<script lang="ts">
  // A checklist (AC or DoD): checkbox + inline markdown per item, markdown between items (DoD), an
  // "Add an item" field at the bottom. The boxes are controlled: what shows is what the card holds.
  import type { Segment } from "../lib/checklist";
  import { t } from "../lib/i18n.svelte";
  import Markdown from "./Markdown.svelte";

  let {
    segments,
    editable,
    busy = false,
    label,
    ontoggle,
    onadd,
  }: {
    segments: Segment[];
    editable: boolean;
    busy?: boolean;
    label: string;
    ontoggle: (line: number) => unknown;
    onadd: (text: string) => Promise<boolean>;
  } = $props();

  let draft = $state("");

  async function add(event: SubmitEvent) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || busy) return;
    if (await onadd(text)) draft = "";
  }

  function toggle(event: MouseEvent, line: number) {
    event.preventDefault(); // the box changes when the card does
    if (editable && !busy) ontoggle(line);
  }
</script>

<div class="checklist" role="group" aria-label={label}>
  {#each segments as segment, i (segment.type === "item" ? `l${segment.line}` : `t${i}`)}
    {#if segment.type === "item"}
      <label class="item" class:done={segment.checked}>
        <input type="checkbox" checked={segment.checked} disabled={!editable} onclick={(e) => toggle(e, segment.line)} />
        <Markdown text={segment.text} inline />
      </label>
    {:else}
      <div class="between"><Markdown text={segment.text} /></div>
    {/if}
  {/each}
  {#if editable}
    <form class="add" onsubmit={add}>
      <input type="text" placeholder={t("checklist.add")} aria-label={t("checklist.add")} disabled={busy} bind:value={draft} />
    </form>
  {/if}
</div>

<style>
  .checklist { display: grid; gap: var(--sp-6); }
  .item { display: flex; gap: var(--sp-10); align-items: flex-start; padding: var(--sp-6) var(--sp-2); cursor: pointer; }
  .item input { width: 18px; height: 18px; margin: var(--sp-2) 0 0; flex: none; accent-color: var(--accent); }
  .item.done :global(.md) { color: var(--muted); }
  .between { color: var(--muted); font-size: var(--fs-ui); padding-left: 28px; }
  .add input { background: transparent; border-style: dashed; }
</style>
