<script lang="ts">
  // The new card's form, in a sheet. The key fields — the title, the project (the one this
  // person used last) and the description; under "More" what an edit could set: the assignee, the labels, the
  // acceptance criteria, the parent, the dependencies and — with `settings` on the project — the
  // model, the effort and the council. The labels and the cards are picked from lists with search,
  // never typed as text. Ctrl/Cmd+Enter creates. A created card leaves the form open on the same
  // project with the title, the description and the criteria cleared and a link to the card: tasks are put one
  // after another. Unsent text is kept for the tab: closing the sheet (Escape, a click beside it) loses nothing.
  import { tick, untrack } from "svelte";
  import { apiPost } from "../lib/api";
  import { errorText, t } from "../lib/i18n.svelte";
  import { emptyDraft, hasMore, keepDraft, keptDraft, lastProject, newCardBody, rememberProject } from "../lib/newcard";
  import { navigate } from "../lib/router.svelte";
  import { session } from "../lib/session.svelte";
  import { projects } from "../lib/stores/projects.svelte";
  import type { CardCompact } from "../lib/types";
  import ChipMenu from "./ChipMenu.svelte";
  import CouncilChip from "./CouncilChip.svelte";
  import ModelChips from "./ModelChips.svelte";
  import Picker from "./Picker.svelte";
  import Sheet from "./Sheet.svelte";

  let {
    project,
    cards,
    onclose,
  }: {
    /** The board's project filter: where the form opens when this person has no project of their own yet. */
    project: string;
    /** The cards this person sees: what the parent, the dependencies and the labels are picked from. */
    cards: CardCompact[];
    onclose: () => void;
  } = $props();

  const ROUTE = "route"; // the assignee menu's «as the project routes»: no assignee is named so (they start with @)
  const user = session.me?.id ?? "";
  const creatable = $derived(projects.list.filter((p) => session.can("cards.create", p.id)));
  const can = (id: string) => creatable.some((p) => p.id === id);
  const first = () => [lastProject(user), project].find(can) ?? creatable[0]?.id ?? "";

  let draft = $state(untrack(() => keptDraft(user) ?? emptyDraft(first())));
  const moreOpen = untrack(() => hasMore(draft)); // a kept draft's "More" is in view: nothing goes unseen
  let form = $state<HTMLFormElement>();
  let criterion = $state("");
  let creating = $state(false);
  let error = $state("");
  let made = $state<{ id: string; title: string } | null>(null);
  let titleInput = $state<HTMLInputElement>();

  const route = $derived(projects.list.find((p) => p.id === draft.project)?.route ?? "");
  // the model, the effort and the council are decisions about spend: `settings` on the card's project
  const canSpend = $derived(session.can("settings", draft.project));
  const assigneeOptions = $derived([
    { value: ROUTE, text: t("newcard.route", { agent: route }) },
    { value: "", text: t("card.unassigned") },
    ...projects.assignees.map((a) => ({ value: a, text: a })),
  ]);
  // what the lists offer: the cards not cancelled, the newest first, and their labels (with the ones chosen
  // here — a new label stays in its list)
  const live = $derived(cards.filter((c) => c.status !== "Cancelled").sort((a, b) => b.n - a.n));
  // the cards created here since the form opened are offered too, first: the next one's parent or dependency
  let added = $state<{ id: string; title: string }[]>([]);
  const cardOptions = $derived(
    [...added, ...live.filter((c) => !added.some((a) => a.id === c.id))].map((c) => ({ value: c.id, text: `${c.id} · ${c.title}` })),
  );
  const labelOptions = $derived(
    [...new Set([...live.flatMap((c) => c.labels), ...draft.labels])].sort().map((label) => ({ value: label, text: label })),
  );

  $effect(() => {
    // the project went away under the form (its right taken, the project deleted): the next one the person has
    if (creatable.length && !can(draft.project)) draft.project = first();
  });
  $effect(() => keepDraft(user, $state.snapshot(draft)));

  /** `node` is in a sheet opened over the form (the council's editor). Its markup is inside the form's element,
   * so the browser counts its fields as the form's: Enter in them is not this form's «create». */
  function elsewhere(node: EventTarget | null): boolean {
    const sheet = node instanceof Element ? node.closest("dialog") : null;
    return sheet !== null && sheet !== form?.closest("dialog");
  }

  function addCriterion() {
    if (criterion.trim()) draft.acceptance.push(criterion.trim());
    criterion = "";
  }

  /** Enter in a list's field adds the item; with Ctrl/Cmd it is the form's «create». */
  function onItemKey(event: KeyboardEvent, add: () => void) {
    if (event.key !== "Enter" || event.ctrlKey || event.metaKey) return;
    event.preventDefault();
    add();
  }

  async function create(event?: Event) {
    event?.preventDefault();
    if (event && elsewhere(document.activeElement)) return;
    if (creating || !draft.title.trim() || !draft.project) return;
    addCriterion(); // typed, not added yet: it goes with the card
    creating = true;
    error = "";
    made = null; // the notice is of the last card created: not shown beside a refusal of the next
    try {
      const spend = canSpend ? {} : { model: "", effort: "", council: {} };
      const card = await apiPost<CardCompact>("/api/cards", newCardBody({ ...$state.snapshot(draft), ...spend }));
      made = { id: card.id, title: card.title };
      added.unshift(made);
      rememberProject(user, draft.project);
      // the card's own text goes with it; the project and the rest of "More" stay for the next one
      draft.title = "";
      draft.description = "";
      draft.acceptance = [];
      await tick();
      titleInput?.focus();
    } catch (exc) {
      error = errorText(exc);
    } finally {
      creating = false;
    }
  }

  function onKey(event: KeyboardEvent) {
    if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey) || elsewhere(event.target)) return;
    event.preventDefault();
    void create();
  }

  function openMade(event: MouseEvent) {
    if (!made) return;
    event.preventDefault();
    const id = made.id;
    onclose();
    navigate(`/cards/${id}`);
  }
</script>

<Sheet title={t("board.new")} {onclose}>
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
  <form class="new" bind:this={form} onsubmit={create} onkeydown={onKey}>
    {#if made}
      <p class="made" role="status">
        {t("newcard.created")} <a href="/cards/{made.id}" onclick={openMade}>{made.id} · {made.title}</a>
      </p>
    {/if}
    <label class="field">
      <span>{t("newcard.name")}</span>
      <!-- svelte-ignore a11y_autofocus -->
      <input type="text" maxlength="120" autocomplete="off" autofocus bind:value={draft.title} bind:this={titleInput} />
    </label>
    <label class="field">
      <span>{t("board.new_project")}</span>
      <select bind:value={draft.project} onchange={() => (draft.assignee = null)}>
        {#each creatable as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
      </select>
    </label>
    <label class="field">
      <span>{t("newcard.description")}</span>
      <textarea rows="6" bind:value={draft.description}></textarea>
    </label>

    <details class="more" open={moreOpen}>
      <summary>{t("newcard.more")}</summary>
      <div class="extra">
        <dl class="props">
          <dt>{t("card.assignee")}</dt>
          <dd>
            <ChipMenu
              label={t("card.assignee")}
              value={draft.assignee ?? ROUTE}
              options={assigneeOptions}
              onchange={(value) => (draft.assignee = value === ROUTE ? null : value)}
            />
          </dd>
          {#if canSpend}
            <ModelChips card={draft} editable onchange={(changes) => Object.assign(draft, changes)} />
            <CouncilChip card={draft} editable onchange={({ council }) => (draft.council = council)} />
          {/if}
        </dl>
        <div class="field" role="group" aria-label={t("card.labels")}>
          <span>{t("card.labels")}</span>
          <Picker label={t("card.labels")} options={labelOptions} value={draft.labels} multiple create onchange={(labels) => (draft.labels = labels)} />
        </div>
        <div class="field" role="group" aria-label={t("card.acceptance")}>
          <span>{t("card.acceptance")}</span>
          {#if draft.acceptance.length}
            <ul class="items">
              {#each draft.acceptance as text, i (i)}
                <li>
                  <span>{text}</span>
                  <button type="button" class="iconbtn" aria-label={t("newcard.ac_remove")} onclick={() => draft.acceptance.splice(i, 1)}>
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
                  </button>
                </li>
              {/each}
            </ul>
          {/if}
          <input
            type="text"
            autocomplete="off"
            aria-label={t("newcard.ac_add")}
            placeholder={t("newcard.ac_add")}
            bind:value={criterion}
            onkeydown={(event) => onItemKey(event, addCriterion)}
          />
        </div>
        <div class="field" role="group" aria-label={t("card.parent")}>
          <span>{t("card.parent")}</span>
          <Picker label={t("card.parent")} options={cardOptions} value={draft.parent ? [draft.parent] : []} onchange={(ids) => (draft.parent = ids[0] ?? "")} />
        </div>
        <div class="field" role="group" aria-label={t("card.dependencies")}>
          <span>{t("card.dependencies")}</span>
          <Picker label={t("card.dependencies")} options={cardOptions} value={draft.dependencies} multiple onchange={(ids) => (draft.dependencies = ids)} />
        </div>
      </div>
    </details>

    {#if error}<p class="error" role="alert">{error}</p>{/if}
    <div class="actions">
      <span class="hint muted small">{t("newcard.hint")}</span>
      <button class="btn primary" type="submit" disabled={creating || !draft.title.trim()}>{t("action.create")}</button>
    </div>
  </form>
</Sheet>

<style>
  .new { display: grid; gap: var(--sp-12); min-width: 0; }
  .new input[type="text"], .new textarea, .new select { width: 100%; min-width: 0; }
  .made { color: var(--good); }
  .made a { color: var(--accent); }
  .more summary { cursor: pointer; color: var(--muted); font-size: var(--fs-md); }
  .extra { display: grid; gap: var(--sp-12); padding-top: var(--sp-12); }
  .props { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: var(--sp-6) var(--sp-14); margin: 0; align-items: center; font-size: var(--fs-ui); }
  .props :global(dt) { margin: 0; font-size: var(--fs-xs); text-transform: uppercase; letter-spacing: .08em; color: var(--muted); }
  .props :global(dd) { margin: 0; min-width: 0; display: flex; flex-wrap: wrap; gap: var(--sp-6); align-items: center; }
  .items { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--sp-4); }
  .items li { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-8); min-width: 0; }
  .items li span { min-width: 0; overflow-wrap: anywhere; }
  .error { color: var(--crit); }
  .actions { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-12); }
  .actions .btn { flex: 0 0 auto; }
  @media (max-width: 899px) { .hint { display: none; } }
</style>
