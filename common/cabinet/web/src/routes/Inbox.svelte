<script lang="ts">
  // What waits for the person: open crit incidents nobody acknowledged or muted (first), failed deploys that
  // woke cards waiting for them, decisions (several
  // of one type signed at once), agents' questions, cards in Review (several accepted or sent back at once, one card
  // at a time with its own revision).
  import { onMount } from "svelte";
  import ActionComment from "../components/ActionComment.svelte";
  import CardRow from "../components/CardRow.svelte";
  import Freshness from "../components/Freshness.svelte";
  import Markdown from "../components/Markdown.svelte";
  import SignDialog from "../components/SignDialog.svelte";
  import { riskKey } from "../components/PlanSteps.svelte";
  import { coalesce } from "../lib/coalesce";
  import { hasMore, headline } from "../lib/headline";
  import { approveDecisions, withPasskey } from "../lib/decide";
  import { errorText, t, type Key } from "../lib/i18n.svelte";
  import { live, onLiveEvent, watchAll } from "../lib/live.svelte";
  import { navigate } from "../lib/router.svelte";
  import { groupHref } from "../lib/incidentGroups";
  import { checkName, incidentHref } from "../lib/servers";
  import { session } from "../lib/session.svelte";
  import { actMany, bundleRows, canSelect, groupByType, inbox, rowCount, type InboxRow, type TransitionResult } from "../lib/stores/inbox.svelte";
  import { projects } from "../lib/stores/projects.svelte";
  import { synced } from "../lib/synced.svelte";
  import { age } from "../lib/time";
  import { toast } from "../lib/toast.svelte";
  import { trustTitle } from "../lib/trust";
  import type { CardCompact } from "../lib/types";

  let selecting = $state(false);
  let picked = $state<InboxRow[]>([]);
  let pickedCards = $state<CardCompact[]>([]);
  let signing = $state(false);
  let busy = $state(false);
  let results = $state<TransitionResult[]>([]);

  const groups = $derived(groupByType(inbox.decisions));
  const total = $derived(
    rowCount(inbox.decisions) + inbox.questions.length + inbox.review.length + inbox.incidents.length + inbox.deploys.length,
  );
  const stepUp = $derived(picked.some((row) => row.decision.step_up));
  const movable = (card: CardCompact) => session.can("cards.transition", card.project);
  const anyMovable = $derived(inbox.review.some(movable));

  // a failed read is retried (the stale strip says so); a resync of the app reads it again
  const data = synced(
    async () => {
      await inbox.load();
      const open = new Set(inbox.decisions.map((row) => row.decision.id));
      picked = picked.filter((row) => open.has(row.decision.id));
      const review = new Set(inbox.review.map((card) => card.id));
      pickedCards = inbox.review.filter((card) => review.has(card.id) && pickedCards.some((p) => p.id === card.id));
    },
    { onError: (exc) => toast(errorText(exc), "error") },
  );
  const reload = () => data.run();
  const refresh = coalesce(reload);

  // without the live link nothing tells the page what changed: it reads itself once a minute
  $effect(() => {
    if (live.connected) return;
    const tick = setInterval(reload, 60_000);
    return () => clearInterval(tick);
  });

  onMount(() => {
    void reload();
    const release = watchAll();
    const stop = onLiveEvent((event) => {
      if (!event.kind.startsWith("decision.") && !event.kind.startsWith("card.") && !event.kind.startsWith("incident.")
        && !event.kind.startsWith("monitor_trust.") && !event.kind.startsWith("deploy.") && event.kind !== "wait.rejected") {
        return;
      }
      refresh();
    });
    return () => {
      stop();
      refresh.cancel();
      release();
      data.dispose();
    };
  });

  function toggleSelecting() {
    selecting = !selecting;
    picked = [];
    pickedCards = [];
    results = [];
  }

  function pick(row: InboxRow) {
    const on = picked.some((p) => p.decision.id === row.decision.id);
    if (on) picked = picked.filter((p) => p.decision.id !== row.decision.id);
    else if (canSelect(picked, row)) picked = [...picked, row];
  }

  // a bundle of accepts is one row — picked, signed and cleared together
  function pickAll(members: InboxRow[]) {
    const ids = new Set(members.map((m) => m.decision.id));
    if (members.every((m) => picked.some((p) => p.decision.id === m.decision.id))) {
      picked = picked.filter((p) => !ids.has(p.decision.id));
      return;
    }
    for (const m of members) if (!picked.some((p) => p.decision.id === m.decision.id) && canSelect(picked, m)) picked = [...picked, m];
  }

  function signBundle(members: InboxRow[]) {
    picked = members.filter((m) => canSelect([], m));
    if (picked.length) signing = true;
  }

  const bundleTitle = (members: InboxRow[]) =>
    t("inbox.bundle", { n: members.length, servers: new Set(members.map((m) => m.decision.payload?.server)).size });

  function pickCard(card: CardCompact) {
    const on = pickedCards.some((p) => p.id === card.id);
    pickedCards = on ? pickedCards.filter((p) => p.id !== card.id) : [...pickedCards, card];
  }

  async function approvePicked() {
    if (stepUp) {
      signing = true;
      return;
    }
    busy = true;
    try {
      await approveDecisions(picked, withPasskey);
      picked = [];
      await reload();
    } catch (exc) {
      toast(errorText(exc), "error");
    } finally {
      busy = false;
    }
  }

  // accept, or return with one comment for all the picked cards (what to rework) — each card's own action
  let returning = $state(false);

  async function act(action: string, comment?: string) {
    busy = true;
    returning = false;
    try {
      results = await actMany(pickedCards, action, comment);
      pickedCards = [];
      await reload();
    } finally {
      busy = false;
    }
  }

  function resultText(result: TransitionResult): string {
    if (result.outcome === "ok") return t("batch.result.ok", { card: result.card });
    if (result.outcome === "conflict") return t("batch.result.conflict", { card: result.card });
    return t("batch.result.error", { card: result.card, error: errorText(result.error) });
  }

  function subtitle(row: InboxRow): string {
    const parts: string[] = [];
    const v = row.view;
    if (v.steps) parts.push(t("inbox.steps", { count: v.steps.length }));
    const levels = (v.steps ?? []).map((s) => s.risk.level).concat(v.risk ? [v.risk.level] : []);
    const worst = ["high", "medium", "low"].find((level) => levels.includes(level));
    if (worst) parts.push(t(riskKey(worst)));
    if (row.decision.type === "admin_critical") parts.push(t("decision.once"));
    parts.push(row.decision.requested_by.replace(/^(agent|user):/, "@"));
    if (row.decision.type === "monitor_trust" && v.text) parts.push(v.text); // the sysadmin's why
    return parts.join(" · ");
  }

  const titleOf = (row: InboxRow) =>
    row.decision.type === "monitor_trust" // no card — what becomes trusted
      ? trustTitle({ ...row.decision.payload, incident: row.view.incident ?? row.decision.payload.incident })
      : row.view.intent || row.view.title || row.card?.title || row.decision.card;

  // a decision without a card (the monitor's trust) opens on its own page
  const open = (row: InboxRow) =>
    navigate(row.decision.card ? `/cards/${row.decision.card}?decision=${row.decision.id}` : `/decisions/${row.decision.id}`);

  // an agent's question is one short line in the list: its text unfolds on "Show", in full on the card
  let shown = $state<string[]>([]);
  const unfold = (row: InboxRow) =>
    (shown = shown.includes(row.decision.id) ? shown.filter((id) => id !== row.decision.id) : [...shown, row.decision.id]);
</script>

<div class="topbar">
  {#if session.can("servers.read")}<a class="btn small ghost" href="/incidents">{t("mon.full_queue")} →</a>{/if}
  <h1>{t("nav.inbox")}</h1>
  {#if total}<span class="chip crit num">{total}</span>{/if}
  <Freshness {data} />
  <span class="spacer"></span>
  {#if inbox.decisions.length || anyMovable}
    <button class="btn" type="button" onclick={toggleSelecting}>{t(selecting ? "action.cancel" : "action.select")}</button>
  {/if}
</div>

{#if inbox.loaded && total === 0}
  <p class="empty">{t("inbox.empty")}</p>
{:else if !inbox.loaded && !data.error}
  <p class="empty">{t("app.loading")}</p>
{/if}

{#if inbox.incidents.length}
  <!-- open crit nobody acknowledged or muted; ack, mute or resolve takes it away -->
  <div class="section-h">{t("inbox.incidents")} <span class="n">{inbox.incidents.length}</span></div>
  <div class="list">
    {#each inbox.incidents as incident (incident.id)}
      <!-- one tick's crit on several servers is one row; it opens the incidents of the group -->
      <a class="item incident" href={incident.group && incident.members ? groupHref(incident.group.id) : incidentHref(incident.id)}>
        <span class="kind crit" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M12 3l9 16H3z" /><path d="M12 10v4M12 17v.5" /></svg>
        </span>
        <span>
          {#if incident.group && incident.members}
            <span class="t">{incident.id} · {t("mon.group.servers", { n: incident.servers?.length ?? 0 })}</span>
            <span class="s">{checkName(incident.check, incident.group.subjects > 1 ? "" : incident.subject)} · {t("mon.group.count", { n: incident.members.length })}</span>
          {:else}
            <span class="t">{incident.id} · {incident.server}</span>
            <span class="s">{checkName(incident.check, incident.subject)}: {incident.value}</span>
          {/if}
        </span>
        <span class="age">{age(incident.opened_at)}</span>
      </a>
    {/each}
  </div>
{/if}

{#if inbox.deploys.length}
  <!-- a failed run that woke the cards waiting for it; «Got it» takes it away -->
  <div class="section-h">{t("inbox.deploys")} <span class="n">{inbox.deploys.length}</span></div>
  <div class="list">
    {#each inbox.deploys as alert (`${alert.project}:${alert.run}`)}
      <div class="item">
        <span class="kind crit" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M12 3l9 16H3z" /><path d="M12 10v4M12 17v.5" /></svg>
        </span>
        <span>
          <span class="t">{t("deploys.failed_run", { project: alert.project, env: alert.env, number: alert.number || alert.run })}</span>
          <span class="s">
            {#if alert.url}<a href={alert.url} target="_blank" rel="noopener noreferrer">{t("deploys.log")}</a>{/if}
            {#each alert.cards as card (card)} · <a href={`/cards/${card}`}>{card}</a>{/each}
          </span>
        </span>
        <button class="btn small" type="button" onclick={() => inbox.seen(alert).catch((exc) => toast(errorText(exc), "error"))}>
          {t("deploys.seen")}
        </button>
      </div>
    {/each}
  </div>
{/if}

{#if inbox.decisions.length}
  <div class="section-h">{t("inbox.decisions")} <span class="n">{rowCount(inbox.decisions)}</span></div>
  {#each groups as group (group.type)}
    <div class="list">
      {#each bundleRows(group.rows) as row (row.decision.id)}
        {@const members = row.members ?? [row]}
        {@const on = members.every((m) => picked.some((p) => p.decision.id === m.decision.id))}
        {@const allowed = on || members.every((m) => canSelect(picked, m))}
        <button
          type="button"
          class="item"
          class:checked={on}
          class:dim={selecting && !allowed}
          aria-pressed={selecting ? on : undefined}
          onclick={() => (selecting ? (row.members ? pickAll(row.members) : pick(row)) : row.members ? signBundle(row.members) : open(row))}
        >
          {#if selecting}
            <span class="cb" aria-hidden="true">{on ? "✓" : ""}</span>
          {:else}
            <span class="kind {row.decision.type === 'admin_critical' ? 'crit' : 'sign'}" aria-hidden="true">
              <svg viewBox="0 0 24 24"><path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z" /><path d="M9 12l2 2 4-4" /></svg>
            </span>
          {/if}
          <span>
            <span class="t">{row.members ? bundleTitle(row.members) : titleOf(row)}</span>
            <span class="s">{subtitle(row)}</span>
            <span class="meta">
              <span class="chip">{t(`decision.type.${row.decision.type}` as Key)}</span>
              {#if row.decision.card}<span class="chip">{row.decision.card}</span>{/if}
              {#if row.decision.project}<span class="chip">{projects.name(row.decision.project)}</span>{/if}
              {#if row.view.stale}<span class="chip warn">{t("decision.stale")}</span>{/if}
            </span>
          </span>
          <span class="age">{age(row.decision.created)}</span>
        </button>
      {/each}
    </div>
  {/each}
{/if}

{#if inbox.questions.length}
  <div class="section-h">{t("inbox.questions")} <span class="n">{inbox.questions.length}</span></div>
  <div class="list">
    {#each inbox.questions as row (row.decision.id)}
      {@const text = row.view.text ?? ""}
      {@const unfolded = shown.includes(row.decision.id)}
      <div class="item question">
        <span class="kind q" aria-hidden="true">
          <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6V14M12 17v.5" /></svg>
        </span>
        <div class="q-body">
          <button type="button" class="open" onclick={() => open(row)}>
            <span class="t line">{headline(text) || row.decision.card}</span>
            <span class="s line">{row.decision.requested_by.replace(/^(agent|user):/, "@")} · {row.card?.title ?? row.decision.card}</span>
          </button>
          {#if hasMore(text)}
            <button type="button" class="btn small ghost fold" aria-expanded={unfolded} onclick={() => unfold(row)}>
              {t(unfolded ? "inbox.question.hide" : "inbox.question.show")}
            </button>
            {#if unfolded}<div class="full"><Markdown {text} /></div>{/if}
          {/if}
        </div>
        <span class="age">{age(row.decision.created)}</span>
      </div>
    {/each}
  </div>
{/if}

{#if inbox.review.length}
  <div class="section-h">{t("inbox.review")} <span class="n">{inbox.review.length}</span></div>
  <div class="list">
    {#each inbox.review as card (card.id)}
      {#if selecting && movable(card)}
        {@const on = pickedCards.some((p) => p.id === card.id)}
        <button type="button" class="item" class:checked={on} aria-pressed={on} onclick={() => pickCard(card)}>
          <span class="cb" aria-hidden="true">{on ? "✓" : ""}</span>
          <span>
            <span class="t">{card.title}</span>
            <span class="meta"><span class="chip">{card.id}</span><span class="chip">{projects.name(card.project)}</span></span>
          </span>
          <span class="age">{age(card.updated)}</span>
        </button>
      {:else}
        <CardRow {card} />
      {/if}
    {/each}
  </div>
{/if}

{#if results.length}
  <div class="results panel" role="status">
    {#each results as result (result.card)}
      <p class={result.outcome === "ok" ? "ok-text" : "error-text"}>{resultText(result)}</p>
    {/each}
  </div>
{/if}

{#if selecting && returning && pickedCards.length}
  <div class="returning">
    <ActionComment label={t("flow.return.comment")} {busy} onsend={(text) => void act("return", text)} oncancel={() => (returning = false)} />
  </div>
{/if}

{#if selecting && (picked.length || pickedCards.length)}
  <div class="signbar">
    {#if picked.length}
      <span>{t("inbox.selected", { count: picked.length })}</span>
      <button class="btn primary" type="button" disabled={busy} onclick={approvePicked}>
        {t(stepUp ? "inbox.sign_n" : "inbox.approve_n", { count: picked.length })}
      </button>
    {:else}
      <span>{t("inbox.selected", { count: pickedCards.length })}</span>
      <span class="spacer"></span>
      <button class="btn" type="button" disabled={busy} onclick={() => (returning = true)}>{t("action.return")}</button>
      <button class="btn primary" type="button" disabled={busy} onclick={() => void act("accept")}>{t("action.accept")}</button>
    {/if}
  </div>
{/if}

{#if signing}
  <SignDialog
    items={picked.map((row) => ({ decision: row.decision, view: row.view }))}
    onclose={() => ((signing = false), selecting || (picked = []))}
    ondone={() => ((signing = false), (picked = []), void reload())}
    onreread={() => ((signing = false), void reload())}
  />
{/if}

<style>
  /* on a phone the queue link, the heading and «Выбрать» wrap instead of widening the page; a wrapped
     «Выбрать» keeps to the right */
  .topbar { flex-wrap: wrap; }
  .topbar > .btn:last-child { margin-left: auto; }
  .list { display: flex; flex-direction: column; gap: 8px; margin-bottom: 8px; }
  .item {
    display: grid; grid-template-columns: auto 1fr auto; gap: 10px; align-items: start; text-align: left; width: 100%;
    background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 12px; color: var(--text);
  }
  .item.dim { opacity: .45; }
  a.item.incident { text-decoration: none; border-color: var(--crit); }
  .kind { width: 34px; height: 34px; border-radius: 8px; display: grid; place-items: center; background: var(--surface-2); color: var(--muted); }
  .kind svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
  .kind.sign { color: var(--warn); } .kind.crit { color: var(--crit); } .kind.q { color: var(--info); }
  .t { display: block; font-weight: 600; line-height: 1.3; }
  .s { display: block; color: var(--muted); font-size: 13px; margin-top: 2px; }
  .meta { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 7px; }
  .age { color: var(--muted); font-size: 12px; white-space: nowrap; }
  /* a question: the title and the line under it never wrap, the body unfolds below them */
  .q-body { min-width: 0; display: flex; flex-direction: column; align-items: flex-start; }
  .question .open { all: unset; cursor: pointer; display: block; width: 100%; min-width: 0; }
  .question .open:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
  .line { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .fold { margin-top: 4px; padding-left: 0; }
  .full { margin-top: 6px; font-size: 14px; width: 100%; }
  .cb { width: 20px; height: 20px; border-radius: 5px; border: 1.5px solid var(--line); display: grid; place-items: center; margin-top: 7px; font-size: 13px; font-weight: 700; }
  .item.checked .cb { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
  .results { display: grid; gap: 4px; margin: 10px 0; }
  .results p { margin: 0; }
  .signbar {
    position: fixed; left: 16px; right: 16px; bottom: calc(var(--nav-h) + env(safe-area-inset-bottom, 0px) + 12px);
    background: var(--surface-2); border: 1px solid var(--line); border-radius: 12px; padding: 10px 12px;
    display: flex; align-items: center; gap: 10px; z-index: 15; box-shadow: 0 10px 30px rgba(0, 0, 0, .4); flex-wrap: wrap;
  }
  .signbar .spacer { flex: 1; }
  .signbar > .btn.primary:only-of-type { margin-left: auto; }
  @media (min-width: 900px) { .signbar { left: calc(var(--side-w) + 32px); right: 32px; bottom: 20px; max-width: 800px; } }
  /* the shared «what to rework» of a bulk return, just above the bar */
  .returning {
    position: fixed; left: 16px; right: 16px; bottom: calc(var(--nav-h) + env(safe-area-inset-bottom, 0px) + 76px);
    background: var(--surface-2); border: 1px solid var(--line); border-radius: 12px; padding: 10px 12px;
    z-index: 16; box-shadow: 0 10px 30px rgba(0, 0, 0, .4);
  }
  @media (min-width: 900px) { .returning { left: calc(var(--side-w) + 32px); right: 32px; bottom: 84px; max-width: 800px; } }
</style>
