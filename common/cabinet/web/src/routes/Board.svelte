<script lang="ts">
  // The board: "Add" in its header (the new card's form, → Inbox; apart from the filters),
  // project chips, search and "Filters" (status, agent, specialization, label — one menu at any width);
  // columns by status on a desktop, one list by status on a phone. The query lives in the URL, so a
  // filtered board is a link.
  import { onMount, untrack } from "svelte";
  import CardRow from "../components/CardRow.svelte";
  import { statusKey } from "../components/StatusPill.svelte";
  import { coalesce } from "../lib/coalesce";
  import { errorText, t } from "../lib/i18n.svelte";
  import { onLiveEvent, watchAll } from "../lib/live.svelte";
  import { newCardForm } from "../lib/newcardform.svelte";
  import { navigate, router } from "../lib/router.svelte";
  import { session } from "../lib/session.svelte";
  import {
    DONE_DAYS,
    activeFilters,
    board,
    boardStatus,
    byStatus,
    doneSince,
    filterCards,
    type BoardFilters,
  } from "../lib/stores/cards.svelte";
  import { projects } from "../lib/stores/projects.svelte";
  import { synced } from "../lib/synced.svelte";
  import { toast } from "../lib/toast.svelte";
  import { STATUSES } from "../lib/types";

  const query = $derived(router.current.query);
  const project = $derived(query.get("project") ?? "");
  const q = $derived(query.get("q") ?? "");
  const filters = $derived<BoardFilters>({
    assignee: query.get("assignee") ?? "",
    spec: query.get("spec") ?? "",
    label: query.get("label") ?? "",
    status: boardStatus(query.get("status")),
  });
  const shown = $derived(filterCards(board.cards, filters));
  // Done shows the last DONE_DAYS by default: the column would otherwise grow for ever
  let allDone = $state(false);
  const columns = $derived(byStatus(allDone ? shown : doneSince(shown, Date.now()), filters.status));
  const doneHidden = $derived(
    shown.filter((c) => c.status === "Done").length -
      (columns.find((c) => c.status === "Done")?.cards.length ?? 0),
  );
  const activeCount = $derived(activeFilters(filters));
  // the filters open on "Filters" at any width — and are in view on a board that came filtered (a link)
  let filtersOpen = $state(untrack(() => activeCount > 0));
  const assignees = $derived([...new Set(board.cards.map((c) => c.assignee).filter(Boolean))].sort());
  const labels = $derived([...new Set(board.cards.flatMap((c) => c.labels))].sort());
  const specs = $derived(labels.filter((l) => l.startsWith("spec:")));
  const plainLabels = $derived(labels.filter((l) => !l.startsWith("spec:")));
  const creatable = $derived(projects.list.filter((p) => session.can("cards.create", p.id)));

  let search = $state("");

  function setParam(name: string, value: string) {
    const next = new URLSearchParams(query);
    if (value) next.set(name, value);
    else next.delete(name);
    const text = next.toString();
    navigate(text ? `/board?${text}` : "/board", { replace: true });
  }

  // the board's read: the latest filter, retried on failure, read again on a resync of the app
  let asked = { project: "", q: "" };
  const data = synced(() => board.load(asked), { onError: (exc) => toast(errorText(exc), "error") });
  const research = coalesce(() => void data.run());

  $effect(() => {
    if (router.current.name !== "board") return;
    search = q;
    asked = { project, q };
    void data.run();
  });

  onMount(() => {
    projects.ensure().catch((exc) => toast(errorText(exc), "error"));
    const release = watchAll();
    // a card the event names that could not be read: the whole board is read again (and retried); under a
    // search the search is read again, at most once a second under a stream of events
    const stop = onLiveEvent(
      (event) =>
        void board
          .onEvent(event)
          .then((again) => again && research())
          .catch(() => data.run()),
    );
    return () => {
      research.cancel();
      stop();
      release();
      data.dispose();
    };
  });

  function submitSearch(event: SubmitEvent) {
    event.preventDefault();
    setParam("q", search.trim());
  }

</script>

<div class="topbar">
  <h1>{t("nav.board")}</h1>
  <span class="spacer"></span>
  {#if creatable.length}
    <button type="button" class="btn primary" aria-haspopup="dialog" onclick={() => newCardForm.show(project)}>{t("board.add")}</button>
  {/if}
</div>

<div class="filters" role="toolbar">
  <button type="button" class="chip" class:on={!project} onclick={() => setParam("project", "")}>{t("board.all_projects")}</button>
  {#each projects.list as p (p.id)}
    <button type="button" class="chip" class:on={project === p.id} onclick={() => setParam("project", p.id)}>{p.name}</button>
  {/each}
</div>

<div class="tools">
  <form class="search" onsubmit={submitSearch}>
    <input type="search" placeholder={t("board.search")} aria-label={t("board.search")} bind:value={search} />
  </form>
  <button
    type="button"
    class="btn toggle"
    aria-label={activeCount ? t("board.filters_active", { count: activeCount }) : undefined}
    aria-controls="board-filters"
    aria-expanded={filtersOpen}
    onclick={() => (filtersOpen = !filtersOpen)}
  >
    {t("board.filters")}{#if activeCount}<span class="n">{activeCount}</span>{/if}
  </button>
  <div class="extra" id="board-filters" class:open={filtersOpen}>
  <label class="select">
    <span>{t("board.filter.status")}</span>
    <select value={filters.status} aria-label={t("board.filter.status")} onchange={(e) => setParam("status", e.currentTarget.value)}>
      <option value="">{t("board.filter.any")}</option>
      {#each STATUSES as s (s)}<option value={s}>{t(statusKey(s))}</option>{/each}
    </select>
  </label>
  <label class="select">
    <span>{t("board.filter.assignee")}</span>
    <select value={filters.assignee} aria-label={t("board.filter.assignee")} onchange={(e) => setParam("assignee", e.currentTarget.value)}>
      <option value="">{t("board.filter.any")}</option>
      {#each assignees as a (a)}<option value={a}>{a}</option>{/each}
    </select>
  </label>
  <label class="select">
    <span>{t("board.filter.spec")}</span>
    <select value={filters.spec} aria-label={t("board.filter.spec")} onchange={(e) => setParam("spec", e.currentTarget.value)}>
      <option value="">{t("board.filter.any")}</option>
      {#each specs as s (s)}<option value={s}>{s.slice(5)}</option>{/each}
    </select>
  </label>
  <label class="select">
    <span>{t("board.filter.label")}</span>
    <select value={filters.label} aria-label={t("board.filter.label")} onchange={(e) => setParam("label", e.currentTarget.value)}>
      <option value="">{t("board.filter.any")}</option>
      {#each plainLabels as l (l)}<option value={l}>{l}</option>{/each}
    </select>
  </label>
  </div>
</div>

{#if !board.loading && shown.length === 0}
  <p class="empty">{t("board.empty")}</p>
{:else}
  <div class="cols" class:one={!!filters.status}>
    {#each columns as column (column.status)}
      <section class="col" class:none={column.cards.length === 0}>
        <h3>
          {t(statusKey(column.status))} <span class="n">{column.cards.length}</span>
          {#if column.status === "Done" && (doneHidden > 0 || allDone)}
            <button type="button" class="more" onclick={() => (allDone = !allDone)}>
              {allDone ? t("board.done_recent", { days: DONE_DAYS }) : t("board.done_all", { count: column.cards.length + doneHidden })}
            </button>
          {/if}
        </h3>
        <div class="list">
          {#each column.cards as card (card.id)}<CardRow {card} status="phone" />{/each}
        </div>
      </section>
    {/each}
  </div>
{/if}

<style>
  .filters { display: flex; gap: 6px; overflow-x: auto; padding: 2px 0 8px; }
  .filters .chip { cursor: pointer; }
  .filters .chip.on { color: var(--text); border-color: var(--accent); }
  .tools { display: flex; gap: 8px; flex-wrap: wrap; align-items: flex-end; margin-bottom: 10px; }
  .search { flex: 1 1 220px; }
  .select { display: grid; gap: 3px; font-size: 12px; color: var(--muted); flex: 0 1 150px; }
  /* the projects and the search in view; the five filters open on «Фильтры» at any width */
  .toggle { flex: none; display: inline-flex; align-items: center; gap: 6px; }
  .toggle .n {
    min-width: 18px; padding: 0 5px; border-radius: 999px; background: var(--accent); color: var(--accent-ink);
    font-size: 12px; line-height: 18px; text-align: center;
  }
  .extra { display: none; flex: 1 1 100%; gap: 8px; flex-wrap: wrap; align-items: flex-end; }
  .extra.open { display: flex; }
  .cols { display: grid; gap: 14px; grid-template-columns: minmax(0, 1fr); }
  .col { min-width: 0; }
  .col.none { display: none; }
  @media (min-width: 900px) {
    .cols { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .col.none { display: block; }
    .cols.one { grid-template-columns: minmax(0, 1fr); }
  }
  @media (min-width: 1300px) { .cols:not(.one) { grid-template-columns: repeat(6, minmax(0, 1fr)); } }
  .col h3 { margin: 0 0 8px; font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); display: flex; gap: 6px; }
  .col h3 .n { color: var(--text); }
  .col h3 .more {
    margin-left: auto; background: none; border: 0; padding: 0; color: var(--accent); font: inherit;
    letter-spacing: inherit; text-transform: inherit; cursor: pointer;
  }
  .list { display: flex; flex-direction: column; gap: 6px; }
</style>
