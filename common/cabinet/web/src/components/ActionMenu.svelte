<script lang="ts">
  // "Actions ▾": a button and a panel under it, rows of a line icon and a text; a key row is tinted.
  // Like ChipMenu, a pick, Escape or a click elsewhere closes it; the arrows move between the rows.
  export interface ActionItem {
    id: string;
    text: string;
    icon: string; // the inner markup of a 24×24 line SVG, as the navigation's icons
    tone?: "good" | "warn";
  }

  let {
    label,
    items,
    disabled = false,
    onpick,
  }: {
    label: string;
    items: ActionItem[];
    disabled?: boolean;
    onpick: (id: string) => void;
  } = $props();

  let open = $state(false);
  let root = $state<HTMLElement>();
  let panel = $state<HTMLElement>();

  function pick(id: string) {
    open = false;
    onpick(id);
  }

  function onKey(event: KeyboardEvent) {
    if (event.key === "Escape" && open) {
      open = false;
      root?.querySelector<HTMLButtonElement>(".trigger")?.focus();
      return;
    }
    if (!open || !panel || !["ArrowDown", "ArrowUp"].includes(event.key)) return;
    event.preventDefault();
    const rows = [...panel.querySelectorAll<HTMLButtonElement>("[role=menuitem]")];
    const at = rows.indexOf(document.activeElement as HTMLButtonElement);
    rows[(at + (event.key === "ArrowDown" ? 1 : rows.length - 1)) % rows.length]?.focus();
  }

  $effect(() => {
    if (!open) return;
    panel?.querySelector<HTMLButtonElement>("[role=menuitem]")?.focus();
    const away = (event: MouseEvent) => {
      if (root && !root.contains(event.target as Node)) open = false;
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  });
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<span class="menu" bind:this={root} onkeydown={onKey}>
  <button type="button" class="btn trigger" class:open aria-haspopup="menu" aria-expanded={open} {disabled} onclick={() => (open = !open)}>
    {label}<span class="caret" aria-hidden="true">▾</span>
  </button>
  {#if open}
    <div class="panel" role="menu" aria-label={label} tabindex="-1" bind:this={panel}>
      {#each items as item (item.id)}
        <button type="button" role="menuitem" class="row {item.tone ?? ''}" onclick={() => pick(item.id)}>
          <svg viewBox="0 0 24 24" aria-hidden="true">{@html item.icon}</svg>
          <span>{item.text}</span>
        </button>
      {/each}
    </div>
  {/if}
</span>

<style>
  .menu { position: relative; display: inline-flex; }
  .trigger { display: inline-flex; align-items: center; gap: 6px; }
  .trigger.open { border-color: var(--accent); }
  .caret { font-size: 10px; color: var(--muted); }
  .panel {
    position: absolute; top: calc(100% + 4px); right: 0; z-index: 30; width: max-content; min-width: 220px;
    max-width: calc(100vw - 32px); display: grid; gap: 2px; padding: 6px; background: var(--surface);
    border: 1px solid var(--line); border-radius: 10px; box-shadow: 0 8px 24px rgba(0, 0, 0, .35);
  }
  .row {
    display: flex; align-items: center; gap: 10px; width: 100%; min-height: 44px; text-align: left;
    background: none; border: 0; border-radius: 8px; padding: 8px 12px; font-size: 14px; color: var(--text);
  }
  .row:hover, .row:focus-visible { background: var(--surface-2); outline: none; }
  .row.good { background: var(--good-bg); color: var(--good); font-weight: 600; }
  .row.warn { background: var(--warn-bg); color: var(--warn); font-weight: 600; }
  .row.good:hover, .row.good:focus-visible, .row.warn:hover, .row.warn:focus-visible { filter: brightness(1.15); }
  svg {
    flex: none; width: 20px; height: 20px; fill: none; stroke: currentColor; stroke-width: 1.8;
    stroke-linecap: round; stroke-linejoin: round;
  }
</style>
