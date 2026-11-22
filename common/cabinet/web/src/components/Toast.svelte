<script lang="ts">
  import { dismiss, toasts } from "../lib/toast.svelte";
</script>

<div class="toasts" role="status" aria-live="polite">
  {#each toasts.items as item (item.id)}
    <button class="toast {item.kind}" type="button" onclick={() => dismiss(item.id)}>{item.text}</button>
  {/each}
</div>

<style>
  .toasts {
    position: fixed; left: 50%; transform: translateX(-50%); z-index: 60; display: grid; gap: 8px;
    bottom: calc(var(--nav-h) + env(safe-area-inset-bottom, 0px) + 16px); width: min(92vw, 440px);
  }
  @media (min-width: 900px) {
    .toasts { bottom: 24px; left: calc(50% + var(--side-w) / 2); }
  }
  .toast {
    text-align: left; background: var(--surface-2); border: 1px solid var(--line); color: var(--text);
    padding: 10px 14px; border-radius: 10px; font-size: 14px; box-shadow: 0 10px 30px rgba(0, 0, 0, .4);
  }
  .toast.ok { border-color: var(--good); }
  .toast.error { border-color: var(--crit); }
</style>
