<script lang="ts">
  // Sanitized markdown (lib/markdown.ts: raw HTML as text, safe links only); a ```mermaid block of a full text is
  // a diagram between its pieces.
  import { renderInline, renderParts } from "../lib/markdown";
  import Diagram from "./Diagram.svelte";

  let { text, inline = false }: { text: string; inline?: boolean } = $props();
  const parts = $derived(inline ? [] : renderParts(text));
</script>

{#if inline}
  <span class="md inline">{@html renderInline(text)}</span>
{:else}
  <div class="md">
    {#each parts as part, index (index)}
      {#if part.kind === "diagram"}<Diagram code={part.code} />{:else}{@html part.html}{/if}
    {/each}
  </div>
{/if}

<style>
  .md { overflow-wrap: anywhere; }
  .md :global(p) { margin: 0 0 8px; }
  .md :global(p:last-child), .md :global(ul:last-child), .md :global(ol:last-child) { margin-bottom: 0; }
  .md :global(ul), .md :global(ol) { margin: 0 0 8px; padding-left: 22px; }
  /* a technical name in a sentence: a chip that reads on every message tint, not only on --surface */
  .md :global(code) { background: color-mix(in srgb, var(--text) 11%, transparent); border-radius: 4px; padding: 1px 5px; }
  .md :global(pre) { background: var(--bg); border-radius: 8px; padding: 8px 10px; overflow-x: auto; margin: 0 0 8px; }
  .md :global(pre code) { background: none; padding: 0; }
  .md :global(blockquote) { margin: 0 0 8px; padding-left: 10px; border-left: 3px solid var(--line); color: var(--muted); }
  /* the parts of a brief or a long answer: three sizes over the 15px text, deeper levels as a quiet label */
  .md :global(:is(h1, h2, h3, h4, h5, h6)) { margin: 16px 0 6px; font-weight: 700; line-height: 1.3; text-wrap: balance; }
  .md :global(:is(h1, h2, h3, h4, h5, h6):first-child) { margin-top: 0; }
  .md :global(h1) { font-size: 19px; }
  .md :global(h2) { font-size: 17px; }
  .md :global(h3) { font-size: 15px; }
  .md :global(:is(h4, h5, h6)) { font-size: 13px; font-weight: 600; color: var(--muted); }
  .md :global(table) { border-collapse: collapse; display: block; overflow-x: auto; }
  /* a cell keeps its words whole (a narrow column would break `403` in two); a table too wide scrolls */
  .md :global(td), .md :global(th) { border: 1px solid var(--line); padding: 4px 8px; overflow-wrap: break-word; }
</style>
