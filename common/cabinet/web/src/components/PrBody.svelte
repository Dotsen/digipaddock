<script lang="ts">
  // A pull request in its decision: the head it is for, its files (the cage ones marked), whether the cage
  // is empty, and the diff — read once, on "Show the diff" — where remarks on lines are written. A PR that moved on
  // shows neither files nor diff: they would be of another head. Where the card has critics of the code
  // a line says how their review of this head ended — a link to the council's block on the card's page.
  // The council's block shows the same body while it holds the decision back: the diff is read from `diffUrl` then.
  import { ApiError, apiGet } from "../lib/api";
  import type { LineComment } from "../lib/diff";
  import { errorText, t, type Key } from "../lib/i18n.svelte";
  import { prodEffect } from "../lib/repo";
  import { toast } from "../lib/toast.svelte";
  import type { DecisionView, PrDiff } from "../lib/types";
  import DiffView from "./DiffView.svelte";

  let {
    id,
    card = "",
    view,
    diffUrl = "",
    commenting = false,
    comments = $bindable([]),
    onchanged = () => {},
  }: {
    /** The decision — or, in a council's block, the council. */
    id: string;
    /** Where the diff is read; the decision's own by default. */
    diffUrl?: string;
    /** The decision's card: where its council's block is. */
    card?: string;
    view: DecisionView;
    commenting?: boolean;
    comments?: LineComment[];
    onchanged?: () => void;
  } = $props();

  const cage = $derived(new Set(view.cage ?? []));
  const review = $derived(view.council ?? null);
  const OPEN = new Set(["running", "needs_writer"]);
  const WARN = new Set(["settled", "limited", "failed", "open"]);
  const reviewState = $derived(review ? (OPEN.has(review.state) ? "open" : review.state) : "");
  const reviewText = $derived(
    review ? t(`decision.pr.council.${reviewState}` as Key, { rejected: review.rejected, lines: review.lines, min_lines: review.min_lines }) : "",
  );

  /** On the card's page the council's block is in the thread: go to it; elsewhere the link opens the card. */
  function toCouncil(event: MouseEvent) {
    const block = review?.council ? document.getElementById(`council-${review.council}`) : null;
    if (!block) return;
    event.preventDefault();
    event.stopPropagation(); // the route stays
    block.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  let diff = $state<PrDiff | null>(null);
  let shown = $state(false);
  let loading = $state(false);

  async function toggle() {
    if (shown) {
      shown = false;
      return;
    }
    if (!diff) {
      loading = true;
      try {
        diff = await apiGet<PrDiff>(diffUrl || `/api/decisions/${id}/diff`);
      } catch (exc) {
        toast(errorText(exc), "error");
        if (exc instanceof ApiError && exc.status === 409) onchanged(); // the PR moved on: show what it is now
        return;
      } finally {
        loading = false;
      }
    }
    shown = true;
  }
</script>

<div class="body grid">
  <p>
    {t("decision.pr.head", { number: view.number ?? "", sha: (view.head ?? "").slice(0, 7) })}
    {#if view.target}· {t("decision.pr.target", { target: view.target })}{/if}
    {#if view.url}· <a href={view.url} target="_blank" rel="noopener noreferrer">{t("decision.pr.forge")}</a>{/if}
  </p>
  {#if view.effect}
    <p class={prodEffect(view.effect) ? "crit" : "warn"}>{t("decision.pr.effect", { effect: view.effect })}</p>
  {/if}
  {#if view.target}
    <p class="muted">
      {view.checks?.length ? t("decision.pr.checks", { list: view.checks.join(", ") }) : t("decision.pr.checks_none")}
    </p>
  {/if}
  {#if review}
    <p class={WARN.has(reviewState) ? "warn" : "muted"}>
      {#if review.council && card}
        <a class="review" href="/cards/{encodeURIComponent(card)}" onclick={toCouncil}>{reviewText}</a>
      {:else}
        {reviewText}
      {/if}
    </p>
  {/if}
  {#if view.files?.length}
    <ul class="files">
      {#each view.files as file (file.path)}
        <li>
          <span class="muted status">{file.status}</span>
          <code>{file.path}</code>
          <span class="count"><span class="plus">+{file.additions}</span> <span class="minus">−{file.deletions}</span></span>
          {#if cage.has(file.path)}<span class="chip crit">{t("decision.pr.cage_file")}</span>{/if}
        </li>
      {/each}
    </ul>
  {/if}
  {#if !view.stale}
    <p>{cage.size ? t("decision.pr.cage_yes", { count: cage.size }) : t("decision.pr.cage_no")}</p>
    {#if view.truncated}<p class="muted">{t("decision.pr.truncated")}</p>{/if}
    <div>
      <button class="btn small" type="button" disabled={loading} aria-expanded={shown} onclick={() => void toggle()}>
        {t(shown ? "decision.pr.hide_diff" : "decision.pr.show_diff")}
      </button>
    </div>
    {#if shown && diff}
      <DiffView text={diff.diff} truncated={diff.truncated} url={view.url ?? ""} {commenting} bind:comments />
    {/if}
  {/if}
</div>

<style>
  .body { padding: 0 14px 10px; margin: 0; font-size: 14px; min-width: 0; }
  .grid { display: grid; gap: 6px; }
  .grid p { margin: 0; }
  .files { list-style: none; margin: 0; padding: 0; display: grid; gap: 3px; font-size: 13px; }
  .files li { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; min-width: 0; }
  .files code { overflow-wrap: anywhere; }
  .status { font-size: 12px; min-width: 5.5em; }
  .count { font-size: 12px; white-space: nowrap; }
  .review { color: inherit; text-decoration: underline dotted; }
  .plus { color: var(--good); }
  .minus { color: var(--crit); }
</style>
