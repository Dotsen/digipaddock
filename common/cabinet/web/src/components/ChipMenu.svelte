<script lang="ts">
  // A value as a chip; a click opens the choices in a small panel under it (a styled dropdown, not the
  // native select the owner found ugly, and not an inline row that made the page jump). A choice, Escape
  // or a click elsewhere closes it.
  export interface Option {
    value: string;
    text: string;
    tone?: string;
  }

  let {
    value,
    options,
    label,
    tone = "",
    disabled = false,
    onchange,
  }: {
    value: string;
    options: Option[];
    label: string;
    tone?: string;
    disabled?: boolean;
    onchange: (value: string) => void;
  } = $props();

  let open = $state(false);
  let root = $state<HTMLElement>();
  const current = $derived(options.find((o) => o.value === value)?.text ?? value);

  function pick(next: string) {
    open = false;
    if (next !== value) onchange(next);
  }

  function onKey(event: KeyboardEvent) {
    if (event.key !== "Escape" || !open) return;
    // the open menu's own Escape: a modal sheet around it (the new card's form) must not take it for its «close»
    event.preventDefault();
    event.stopPropagation();
    open = false;
  }

  $effect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (root && !root.contains(event.target as Node)) open = false;
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  });
</script>

<span class="menu" bind:this={root}>
  <button
    type="button"
    class="chip pick {tone}"
    class:open
    aria-haspopup="listbox"
    aria-expanded={open}
    aria-label={label}
    {disabled}
    onclick={() => (open = !open)}
    onkeydown={onKey}
  >
    {#if tone}<span class="dot"></span>{/if}
    {current}
    <span class="caret" aria-hidden="true">▾</span>
  </button>
  {#if open}
    <div class="panel" role="listbox" aria-label={label} tabindex="-1" onkeydown={onKey}>
      {#each options as o (o.value)}
        <button type="button" role="option" class="row {o.tone ?? ''}" class:on={o.value === value} aria-selected={o.value === value} onclick={() => pick(o.value)}>
          {#if o.tone !== undefined}<span class="dot"></span>{/if}
          {o.text}
        </button>
      {/each}
    </div>
  {/if}
</span>

<style>
  .menu { position: relative; display: inline-flex; }
  .pick { cursor: pointer; color: var(--text); }
  .pick.open { border-color: var(--accent); }
  .caret { font-size: 10px; color: var(--muted); }
  .panel {
    position: absolute; top: calc(100% + 4px); left: 0; z-index: 30; min-width: 160px;
    display: grid; gap: 2px; padding: 6px; background: var(--surface); border: 1px solid var(--line);
    border-radius: 10px; box-shadow: 0 8px 24px rgba(0, 0, 0, .35);
  }
  .row {
    display: flex; align-items: center; gap: 8px; width: 100%; text-align: left; white-space: nowrap;
    background: none; border: 0; border-radius: 6px; padding: 7px 10px; font-size: 14px; color: var(--muted);
    cursor: pointer;
  }
  .row:hover, .row:focus-visible { background: var(--surface-2); color: var(--text); }
  .row.on { color: var(--text); font-weight: 600; }
  .row .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
  .row.good { color: var(--good); } .row.warn { color: var(--warn); } .row.crit { color: var(--crit); }
  .row.acc { color: var(--accent); } .row.info { color: var(--info); }
  .row.on.good, .row.on.warn, .row.on.crit, .row.on.acc, .row.on.info { color: var(--text); }
</style>
