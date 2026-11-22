<script lang="ts">
  // The card's council in its header, after the model and the effort: who criticises its brief and its
  // code — the card's own composition per kind, else its project's. Text for everyone who sees the card; for
  // whoever has `settings` (the owner's spend decision) a chip that opens the editor in a sheet.
  import { effective, same, summary, type Council } from "../lib/council";
  import { t } from "../lib/i18n.svelte";
  import { projects } from "../lib/stores/projects.svelte";
  import type { Card } from "../lib/types";
  import CouncilEditor from "./CouncilEditor.svelte";
  import Sheet from "./Sheet.svelte";

  let {
    card,
    editable,
    saving = false,
    open = $bindable(false),
    onchange,
  }: {
    /** A card, or the draft of one in the new card's form. */
    card: Pick<Card, "project" | "council">;
    editable: boolean;
    saving?: boolean;
    /** The editor is shown: the council's block in the thread opens it too. */
    open?: boolean;
    onchange: (changes: { council: Council }) => void;
  } = $props();

  const own = $derived(card.council ?? {});
  const inherited = $derived(projects.list.find((p) => p.id === card.project)?.council ?? {});
  const text = $derived(summary(effective(own, inherited)));

  function save(council: Council) {
    open = false;
    if (!same(council, own)) onchange({ council }); // nothing changed — no write: the card's revision stays
  }
</script>

<dt>{t("card.council")}</dt>
<dd>
  {#if editable}
    <button type="button" class="chip pick" aria-haspopup="dialog" aria-label={t("card.council")} disabled={saving} onclick={() => (open = true)}>
      {text}
      <span class="caret" aria-hidden="true">▾</span>
    </button>
  {:else}
    {text}
  {/if}
</dd>
{#if open && editable}
  <Sheet title={t("card.council")} onclose={() => (open = false)}>
    <CouncilEditor value={own} {inherited} {saving} onsave={save} />
  </Sheet>
{/if}

<style>
  /* the same label style as the card's own <dt>s (Card.svelte .props dt): scoped styles do not reach a child */
  dt { margin: 0; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); }
  .pick { cursor: pointer; color: var(--text); }
  .caret { font-size: 10px; color: var(--muted); }
</style>
