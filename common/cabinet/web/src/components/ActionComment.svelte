<script lang="ts">
  // A short text a step needs before it is sent: what to rework when a card goes back.
  // «Send» stays off until there is text, unless the text is optional (why a card is reopened);
  // Ctrl/Cmd+Enter sends, Escape cancels.
  import { t } from "../lib/i18n.svelte";

  let {
    label,
    hint = "",
    optional = false,
    busy = false,
    onsend,
    oncancel,
  }: {
    label: string;
    hint?: string;
    optional?: boolean;
    busy?: boolean;
    onsend: (text: string) => void;
    oncancel: () => void;
  } = $props();

  let text = $state("");
  const ready = $derived((optional || text.trim().length > 0) && !busy);

  function send() {
    if (ready) onsend(text.trim());
  }

  function onKey(event: KeyboardEvent) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      send();
    } else if (event.key === "Escape") {
      oncancel();
    }
  }
</script>

<div class="ask">
  {#if hint}<p class="hint">{hint}</p>{/if}
  <!-- svelte-ignore a11y_autofocus -->
  <textarea rows="3" aria-label={label} placeholder={label} bind:value={text} onkeydown={onKey} autofocus maxlength="500"></textarea>
  <div class="row">
    <button type="button" class="btn primary small" disabled={!ready} onclick={send}>{t("action.send")}</button>
    <button type="button" class="btn ghost small" onclick={oncancel}>{t("action.cancel")}</button>
  </div>
</div>

<style>
  .ask { display: grid; gap: 6px; max-width: 560px; width: 100%; }
  .ask textarea { width: 100%; box-sizing: border-box; font: inherit; padding: 8px 10px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); color: var(--text); resize: vertical; }
  .row { display: flex; gap: 8px; }
  .hint { margin: 0; font-size: 13px; color: var(--muted); }
</style>
