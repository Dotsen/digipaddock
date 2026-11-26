<script lang="ts">
  // A count pill (the inbox badge, a chat's new replies); nothing when the count is zero. `label` names what is
  // counted for a screen reader ("BOSS, new messages: 3").
  let { count, label = "" }: { count: number; label?: string } = $props();
</script>

{#if count > 0}
  <span class="badge num" aria-label={label || String(count)}>{count > 99 ? "99+" : count}</span>
{/if}

<style>
  .badge {
    min-width: 18px; height: 18px; padding: 0 5px; border-radius: 9px; background: var(--crit);
    color: #1c1c1c; font-size: var(--fs-xs); font-weight: 700; display: inline-grid; place-items: center;
  }
</style>
