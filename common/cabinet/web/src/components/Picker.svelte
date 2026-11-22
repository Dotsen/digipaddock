<script lang="ts" module>
  export interface PickOption {
    value: string;
    text: string;
  }

  let made = 0; // one id per picker on the page: the field names its list and the row the arrows stand on
</script>

<script lang="ts">
  // A list with search: the field is the search, the choices open under it, the chosen are chips with
  // their own «remove». One value, several (`multiple`: a pick of a chosen one takes it back), or several with
  // repeats (`repeat`: a council's critic named twice reads twice); `create` offers the typed text as a new value
  // (a label the board does not have yet). Inside a modal sheet its Escape and Enter are its own: Escape closes
  // the open list, not the sheet; Enter picks, and never submits the form around it (Ctrl/Cmd+Enter is the form's).
  import { t } from "../lib/i18n.svelte";

  let {
    label,
    options,
    value,
    multiple = false,
    repeat = false,
    create = false,
    max,
    disabled = false,
    chosenText,
    onchange,
  }: {
    /** The accessible name of the search field and of the list. */
    label: string;
    options: PickOption[];
    /** The chosen values, in order; with `repeat` one may be there more than once. */
    value: string[];
    multiple?: boolean;
    repeat?: boolean;
    create?: boolean;
    /** No more picks at this many (several values only). */
    max?: number;
    disabled?: boolean;
    /** How a chosen value reads when it differs from its option's text, or has no option any more. */
    chosenText?: (value: string) => string;
    onchange: (value: string[]) => void;
  } = $props();

  const LIMIT = 30; // rows shown: past it the search is to be narrowed
  const id = `picker-${++made}`;
  const several = $derived(multiple || repeat);

  let query = $state("");
  let open = $state(false);
  let active = $state(-1); // the row the arrows stand on; -1 — none
  let root = $state<HTMLElement>();
  let panel = $state<HTMLElement>();

  const full = $derived(several && max !== undefined && value.length >= max);
  const words = $derived(query.trim().toLowerCase().split(/\s+/).filter(Boolean));
  const matches = $derived(options.filter((o) => words.every((word) => o.text.toLowerCase().includes(word))));
  const typed = $derived(query.trim());
  const fresh = $derived(create && typed !== "" && !options.some((o) => o.value === typed) && (repeat || !value.includes(typed)));
  const rows = $derived([
    ...matches.slice(0, LIMIT).map((o) => ({ value: o.value, text: o.text })),
    ...(fresh ? [{ value: typed, text: t("picker.create", { text: typed }) }] : []),
  ]);
  const text = (chosen: string) => chosenText?.(chosen) ?? options.find((o) => o.value === chosen)?.text ?? chosen;

  function pick(chosen: string) {
    if (full) return;
    query = "";
    active = -1;
    if (!several) {
      open = false;
      if (value.length !== 1 || value[0] !== chosen) onchange([chosen]);
      return;
    }
    const next = repeat || !value.includes(chosen) ? [...value, chosen] : value.filter((v) => v !== chosen);
    // the pick that fills the most allowed switches the field off: its list goes with it, or it would take more
    if (max !== undefined && next.length >= max) open = false;
    onchange(next);
  }

  function onKey(event: KeyboardEvent) {
    if (event.key === "Escape") {
      if (!open) return; // the sheet's, or the page's
      event.preventDefault();
      event.stopPropagation();
      open = false;
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      open = true;
      active = event.key === "ArrowDown" ? Math.min(active + 1, rows.length - 1) : Math.max(active - 1, 0);
    } else if (event.key === "Enter" && !(event.ctrlKey || event.metaKey)) {
      event.preventDefault(); // never the form's submit
      const row = open ? rows[active] : undefined;
      if (row) pick(row.value);
    }
  }

  function onInput() {
    open = true;
    active = typed ? 0 : -1; // Enter takes the first match of what was typed
  }

  $effect(() => {
    if (!open) return;
    panel?.scrollIntoView?.({ block: "nearest" }); // in a sheet the list may open below the fold
    const away = (event: MouseEvent) => {
      if (root && !root.contains(event.target as Node)) open = false;
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  });
</script>

<div class="picker" bind:this={root} onfocusout={(event) => { if (!root?.contains(event.relatedTarget as Node | null)) open = false; }}>
  {#if value.length}
    <ul class="chosen">
      {#each value as chosen, i (i)}
        <li class="chip text">
          {text(chosen)}
          <button type="button" class="drop" aria-label={t("picker.remove", { name: text(chosen) })} {disabled} onclick={() => onchange(value.filter((_, at) => at !== i))}>×</button>
        </li>
      {/each}
    </ul>
  {/if}
  <input
    type="text"
    role="combobox"
    autocomplete="off"
    aria-label={label}
    aria-expanded={open}
    aria-controls="{id}-list"
    aria-autocomplete="list"
    aria-activedescendant={open && active >= 0 && rows[active] ? `${id}-${active}` : undefined}
    placeholder={t("picker.search")}
    disabled={disabled || full}
    bind:value={query}
    onfocus={() => (open = true)}
    onclick={() => (open = true)}
    oninput={onInput}
    onkeydown={onKey}
  />
  {#if open}
    <div class="panel" role="listbox" id="{id}-list" aria-label={label} aria-multiselectable={several} bind:this={panel}>
      {#each rows as row, i (i)}
        <!-- mousedown is kept from the field: the list must not close under the click that picks -->
        <button
          type="button"
          role="option"
          id="{id}-{i}"
          class="row"
          class:at={i === active}
          class:on={!repeat && value.includes(row.value)}
          aria-selected={!repeat && value.includes(row.value)}
          tabindex="-1"
          onmousedown={(event) => event.preventDefault()}
          onclick={() => pick(row.value)}
        >
          {row.text}
        </button>
      {:else}
        <div class="aside" role="presentation">{t("picker.empty")}</div>
      {/each}
      {#if matches.length > LIMIT}
        <div class="aside" role="presentation">{t("picker.more", { count: LIMIT })}</div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .picker { position: relative; display: grid; gap: 6px; min-width: 0; }
  .picker input { width: 100%; min-width: 0; }
  .chosen { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px; min-width: 0; }
  .drop { background: none; border: 0; padding: 0 0 0 4px; color: var(--muted); font: inherit; cursor: pointer; }
  .drop:hover { color: var(--text); }
  .panel {
    position: absolute; top: calc(100% + 4px); left: 0; right: 0; z-index: 30; max-height: 240px; overflow-y: auto;
    display: grid; gap: 2px; padding: 6px; background: var(--surface); border: 1px solid var(--line);
    border-radius: 10px; box-shadow: 0 8px 24px rgba(0, 0, 0, .35);
  }
  .row {
    display: block; width: 100%; text-align: left; background: none; border: 0; border-radius: 6px;
    padding: 7px 10px; font-size: 14px; color: var(--muted); cursor: pointer; overflow-wrap: anywhere;
  }
  .row:hover, .row.at { background: var(--surface-2); color: var(--text); }
  .row.on { color: var(--text); font-weight: 600; }
  .row.on::before { content: "✓ "; color: var(--accent); }
  .aside { padding: 7px 10px; font-size: 12px; color: var(--muted); } /* not the app's framed .note */
</style>
