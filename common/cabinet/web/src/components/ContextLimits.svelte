<script lang="ts">
  // Settings → Models → «Context limit», with `settings`: one row per role of the agent registry — its session
  // context limit in force (the default marked), a number of tokens or the explicit «no limit», saved per role
  // (PUT /api/context-limits/<agent>, journaled with the old and the new value). A value out of range is refused in
  // the owner's words before anything is sent; the server says the same of one that slips past.
  import { onMount } from "svelte";
  import { apiGet, apiPut } from "../lib/api";
  import { LIMIT_DEFAULT, NO_LIMIT, parseLimit, tokens, type ContextLimit, type Limit } from "../lib/contextLimit";
  import { errorText, t } from "../lib/i18n.svelte";
  import { toast } from "../lib/toast.svelte";

  interface Draft {
    text: string;
    none: boolean;
  }

  let rows = $state<ContextLimit[] | null>(null);
  let drafts = $state<Record<string, Draft>>({});
  let busy = $state("");

  const draftOf = (row: ContextLimit): Draft =>
    row.limit === NO_LIMIT ? { text: "", none: true } : { text: String(row.limit), none: false };

  onMount(() => {
    apiGet<ContextLimit[]>("/api/context-limits").then(
      (found) => {
        drafts = Object.fromEntries(found.map((row) => [row.agent, draftOf(row)]));
        rows = found;
      },
      (exc) => {
        rows = [];
        toast(errorText(exc), "error");
      },
    );
  });

  /** What the row's form asks for: tokens, «no limit», or null — the typed value is not one. */
  const wanted = (agent: string): Limit | null =>
    drafts[agent].none ? NO_LIMIT : parseLimit(drafts[agent].text);
  const invalid = (agent: string) => wanted(agent) === null;
  const changed = (row: ContextLimit) => !invalid(row.agent) && wanted(row.agent) !== row.limit;

  /** «No limit» off: the number to start from is the role's own, else the default. */
  function toggle(row: ContextLimit, none: boolean) {
    const draft = drafts[row.agent];
    draft.none = none;
    if (!none && parseLimit(draft.text) === null) draft.text = String(row.limit === NO_LIMIT ? LIMIT_DEFAULT : row.limit);
  }

  async function save(event: SubmitEvent, row: ContextLimit) {
    event.preventDefault();
    const limit = wanted(row.agent);
    if (limit === null || busy) return;
    busy = row.agent;
    try {
      const saved = await apiPut<ContextLimit>(`/api/context-limits/${encodeURIComponent(row.agent)}`, { limit });
      rows = (rows ?? []).map((r) => (r.agent === saved.agent ? saved : r));
      drafts[saved.agent] = draftOf(saved);
      toast(t("context.saved", { agent: saved.agent }), "info");
    } catch (exc) {
      toast(errorText(exc), "error");
    } finally {
      busy = "";
    }
  }

  const current = (row: ContextLimit) =>
    row.limit === NO_LIMIT ? t("context.no_limit") : t("context.tokens", { n: tokens(row.limit) });
</script>

<p class="muted small intro">{t("context.intro")}</p>
{#if rows === null}
  <p class="empty">{t("app.loading")}</p>
{:else}
  <ul class="limits">
    {#each rows as row (row.agent)}
      <li class="panel limit">
        <span class="who">
          <b>@{row.agent}</b>
          <span class="muted small">
            {t("context.now", { value: current(row) })}
            {#if !row.set}<span class="chip">{t("context.default")}</span>{/if}
          </span>
        </span>
        <form class="edit" novalidate onsubmit={(event) => save(event, row)}>
          <input
            type="text"
            inputmode="numeric"
            autocomplete="off"
            spellcheck="false"
            aria-label={t("context.value", { agent: row.agent })}
            aria-invalid={invalid(row.agent)}
            placeholder={drafts[row.agent].none ? "—" : ""}
            disabled={drafts[row.agent].none}
            bind:value={drafts[row.agent].text}
          />
          <label class="switch small">
            <input type="checkbox" checked={drafts[row.agent].none} onchange={(e) => toggle(row, e.currentTarget.checked)} />
            {t("context.none")}
          </label>
          <button class="btn small primary" type="submit" disabled={busy === row.agent || !changed(row)}>{t("action.save")}</button>
        </form>
        {#if invalid(row.agent)}<p class="bad small" role="alert">{t("context.invalid")}</p>{/if}
      </li>
    {/each}
  </ul>
{/if}

<style>
  .intro { margin: var(--sp-12) 0; }
  .limits { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--sp-8); }
  /* one row per role, the same columns in every row: the button never moves with a value's length */
  .limit { display: grid; grid-template-columns: minmax(0, 1fr); gap: var(--sp-8); align-items: center; }
  .who { display: grid; gap: var(--sp-2); min-width: 0; }
  .who .chip { margin-left: var(--sp-4); }
  .edit { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: var(--sp-8); align-items: center; }
  .edit input[type="text"] { font-variant-numeric: tabular-nums; }
  .edit input[aria-invalid="true"] { border-color: var(--crit); }
  .switch { display: inline-flex; gap: var(--sp-6); align-items: center; white-space: nowrap; }
  .small { font-size: var(--fs-sm); }
  .bad { color: var(--crit); margin: 0; }
  @media (min-width: 720px) {
    .limit { grid-template-columns: minmax(0, 1fr) auto; }
    .edit { grid-template-columns: 10rem 9rem auto; }
    .bad { grid-column: 1 / -1; }
  }
</style>
