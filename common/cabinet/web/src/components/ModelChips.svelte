<script lang="ts">
  // The card's model and effort in its header, after the assignee: what the card's sessions run on —
  // its own model (a cabinet model or one of the subscription), else the project's, else the agent's own
  // on the subscription; its own effort, else the project's, else the agent's. Chip menus for whoever has
  // `settings` (the owner's spend decision), text for the rest; a card on a disabled model or on a kind whose key
  // is not on the host is warned here — its next command would fail.
  import { t } from "../lib/i18n.svelte";
  import { models } from "../lib/stores/models.svelte";
  import { projects } from "../lib/stores/projects.svelte";
  import { EFFORTS, type Card } from "../lib/types";
  import ChipMenu from "./ChipMenu.svelte";

  let {
    card,
    editable,
    saving = false,
    onchange,
  }: {
    /** A card, or the draft of one in the new card's form. */
    card: Pick<Card, "project" | "model" | "effort">;
    editable: boolean;
    saving?: boolean;
    onchange: (changes: { model: string } | { effort: string }) => void;
  } = $props();

  const project = $derived(projects.list.find((p) => p.id === card.project));
  const projectModel = $derived(project?.model ?? "");
  const projectEffort = $derived(project?.effort ?? "");
  const cardModel = $derived(card.model ?? "");
  const cardEffort = $derived(card.effort ?? "");
  const modelOptions = $derived([
    { value: "", text: projectModel ? t("card.model.project", { name: models.name(projectModel) }) : t("card.model.subscription") },
    ...models.list.filter((m) => m.enabled || m.id === cardModel).map((m) => ({ value: m.id, text: models.title(m) })),
  ]);
  const effortOptions = $derived([
    { value: "", text: projectEffort ? t("card.effort.project", { level: projectEffort }) : t("card.effort.agent") },
    ...EFFORTS.map((level) => ({ value: level, text: level })),
  ]);
  const effective = $derived(models.list.find((m) => m.id === (cardModel || projectModel)));
  const warning = $derived(
    // a model of the subscription needs no provider key — its session starts as one without a model does
    !effective ? "" : !effective.enabled ? t("card.model.disabled") : !effective.builtin && !effective.key ? t("card.model.no_key") : "",
  );
  const text = (options: { value: string; text: string }[], value: string) => options.find((o) => o.value === value)?.text ?? value;
</script>

<dt>{t("card.model")}</dt>
<dd>
  {#if editable}
    <ChipMenu label={t("card.model")} value={cardModel} options={modelOptions} disabled={saving} onchange={(model) => onchange({ model })} />
  {:else}
    {text(modelOptions, cardModel)}
  {/if}
  {#if warning}<span class="warn">{warning}</span>{/if}
</dd>
<dt>{t("card.effort")}</dt>
<dd>
  {#if editable}
    <ChipMenu label={t("card.effort")} value={cardEffort} options={effortOptions} disabled={saving} onchange={(effort) => onchange({ effort })} />
  {:else}
    {text(effortOptions, cardEffort)}
  {/if}
</dd>

<style>
  /* the same label style as the card's own <dt>s (Card.svelte .props dt): scoped styles do not reach a child */
  dt { margin: 0; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); }
  .warn { color: var(--crit); font-size: 12px; margin-left: 8px; }
</style>
