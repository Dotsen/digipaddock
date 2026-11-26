<script lang="ts">
  // A sheet from below for an action with a form — a native modal dialog, as StatusOverride:
  // Escape and a click on the backdrop close it, the focus goes back where it was.
  import type { Snippet } from "svelte";
  import { t } from "../lib/i18n.svelte";

  let { title, onclose, children }: { title: string; onclose: () => void; children: Snippet } = $props();

  function open(dialog: HTMLDialogElement) {
    const back = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialog.showModal();
    return { destroy() {
      dialog.close();
      document.body.style.overflow = overflow;
      back?.focus();
    } };
  }
</script>

<dialog
  class="sheet action-sheet"
  aria-label={title}
  use:open
  oncancel={(event) => { event.preventDefault(); onclose(); }}
  onclick={(event) => { if (event.target === event.currentTarget) onclose(); }}
>
  <div class="body">
    <div class="head">
      <h2>{title}</h2>
      <button type="button" class="iconbtn" aria-label={t("action.close")} onclick={onclose}>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
    </div>
    {@render children()}
  </div>
</dialog>

<style>
  .action-sheet { margin: auto auto 0; max-height: calc(100dvh - 16px); color: var(--text); padding: 0; }
  .action-sheet::backdrop { background: rgb(0 0 0 / .55); }
  .body { min-width: 0; padding: 18px 18px calc(18px + env(safe-area-inset-bottom, 0px)); display: grid; gap: var(--sp-12); overflow-wrap: anywhere; }
  .head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--sp-12); }
  .body :global(p) { margin: 0; }
  @media (min-width: 900px) { .action-sheet { margin: auto; max-height: calc(100dvh - 32px); } }
</style>
