<script lang="ts">
  // A card: a compact header (status with its manual override, assignee, project, the brief's state that opens
  // its tab; "More" opens the dependencies, parent, due, priority, servers, labels and AC/DoD counts),
  // the next step (what happens, who does it, what blocks it — beside the tabs on a desktop, above
  // them on a phone) and five tabs — the discussion with its decisions, the brief (its versions, the
  // description with the AC and DoD checklists), the result (what is asked to be accepted, the Review
  // iterations), the history (paged), the agents' sessions. The tab is in the address (`?tab=`), a linked
  // decision opens the discussion. Live events of the card update it in place.
  import { onMount, untrack } from "svelte";
  import BackButton from "../components/BackButton.svelte";
  import BriefTab from "../components/BriefTab.svelte";
  import HistoryTab from "../components/HistoryTab.svelte";
  import ResultTab from "../components/ResultTab.svelte";
  import ChipMenu from "../components/ChipMenu.svelte";
  import CouncilChip from "../components/CouncilChip.svelte";
  import ModelChips from "../components/ModelChips.svelte";
  import NextStep from "../components/NextStep.svelte";
  import ProjectChip from "../components/ProjectChip.svelte";
  import { workingAgent } from "../components/RunState.svelte";
  import Sessions from "../components/Sessions.svelte";
  import StatusOverride from "../components/StatusOverride.svelte";
  import StatusPill from "../components/StatusPill.svelte";
  import Thread from "../components/Thread.svelte";
  import { ApiError, apiPost } from "../lib/api";
  import { draftKey } from "../lib/drafts";
  import { errorText, t, type Key } from "../lib/i18n.svelte";
  import { onLiveEvent, subscribeCard } from "../lib/live.svelte";
  import { navigate, router } from "../lib/router.svelte";
  import { session } from "../lib/session.svelte";
  import { brief, briefLabel, briefTone } from "../lib/stores/brief.svelte";
  import { cardPage, checklistCounts } from "../lib/stores/card.svelte";
  import { councils } from "../lib/stores/councils.svelte";
  import { models } from "../lib/stores/models.svelte";
  import { effective } from "../lib/council";
  import { projects } from "../lib/stores/projects.svelte";
  import { runs } from "../lib/stores/sessions.svelte";
  import { thread } from "../lib/stores/thread.svelte";
  import { toast } from "../lib/toast.svelte";

  let { id }: { id: string } = $props();

  const TABS = ["thread", "brief", "result", "history", "sessions"] as const;
  type Tab = (typeof TABS)[number];
  const highlight = $derived(router.current.query.get("decision") ?? "");
  const asked = $derived(router.current.query.get("tab") ?? "");
  const tab = $derived<Tab>(highlight ? "thread" : (TABS as readonly string[]).includes(asked) ? (asked as Tab) : "thread");
  const tabLabel: Record<Tab, Key> = {
    thread: "card.thread",
    brief: "card.brief",
    result: "card.result",
    history: "card.history",
    sessions: "card.sessions",
  };
  /** A tab is a place in the address: a link opens it, «back» leaves the card, not the tab. */
  function show(next: Tab) {
    navigate(next === "thread" ? `/cards/${encodeURIComponent(id)}` : `/cards/${encodeURIComponent(id)}?tab=${next}`, { replace: true });
  }
  const card = $derived(cardPage.card?.id === id ? cardPage.card : null);
  const project = $derived(card?.project ?? "");
  const canTransition = $derived(!!card && session.can("cards.transition", project));
  const canEdit = $derived(
    !!card && session.can("cards.edit", project) && (card.created_by === `@${session.me?.id}` || canTransition),
  );
  const canComment = $derived(!!card && session.can("cards.comment", project));
  const canControl = $derived(!!card && session.can("agents.control", project));
  // the card's model and effort are the owner's spend decision — `settings`, like the models themselves
  const canModel = $derived(!!card && session.can("settings"));
  // the council in force — the card's own per kind, else its project's — is shown to everyone it concerns
  const hasCouncil = $derived(
    !!card && Object.keys(effective(card.council ?? {}, projects.list.find((p) => p.id === card.project)?.council ?? {})).length > 0,
  );
  // the card's gatherings, each a block of the thread; whoever has `settings` cancels an open one there
  // and opens the composition (the chip's editor) from it
  let councilEditor = $state(false);
  async function cancelCouncil(council: string) {
    try {
      await councils.cancel(id, council);
    } catch (exc) {
      toast(errorText(exc), "error");
      void councils.load(id).catch(() => undefined); // it may have ended meanwhile: the block says how
    }
  }
  const cardRuns = $derived(runs.card === id ? runs : null);
  let more = $state(false); // the header's secondary facts
  let now = $state(Date.now()); // «start delayed» and «no heartbeat» appear without an event

  async function continueSession(sessionId: string) {
    try {
      await apiPost(`/api/sessions/${encodeURIComponent(sessionId)}/continue`);
    } catch (exc) {
      toast(errorText(exc), "error");
    }
    void runs.refresh().catch(() => undefined);
  }
  const counts = $derived(card ? checklistCounts(card) : null);
  // the brief's state (R17): the backend derives it from the versions, the analyst's work and questions
  const briefView = $derived(brief.id === id ? brief.view : null);
  // the big buttons are the backend's actions for this person; the page adds none of its own
  const cardActions = $derived(card?.actions ?? []);

  async function act(action: string, comment: string | undefined, briefVersion: number | null) {
    const before = card?.status;
    const agent = cardActions.find((a) => a.id === action)?.agent ?? "";
    if ((await cardPage.act(action, comment, briefVersion)) && action === "start" && cardPage.card?.status === before) {
      toast(t("card.started", { agent }), "ok");
    }
    void runs.refresh().catch(() => undefined);
  }
  const assigneeOptions = $derived([
    { value: "", text: t("card.unassigned") },
    ...projects.assignees.map((a) => ({ value: a, text: a })),
  ]);

  // a card that could not be opened says why with «retry»; a shown card's failed refresh is a toast
  function open(current: string) {
    cardPage.load(current).catch((exc) => {
      if (!(exc instanceof ApiError && exc.status === 404) && cardPage.card) toast(errorText(exc), "error");
    });
  }

  // the page follows `id` only: the stores read and write their own state while they load, and that must
  // not run this effect again (its cleanup closes them)
  $effect(() => {
    const current = id;
    untrack(() => {
      open(current);
      void thread.load(current); // a failure is the thread's error (shown with «retry») and retried
      void brief.load(current); // retried the same way; the indicator waits for it
      void councils.load(current); // retried the same way; the thread shows the reviews as lines meanwhile
      void runs.load(current).catch(() => undefined);
      subscribeCard(current);
    });
    return () => {
      subscribeCard(null);
      cardPage.close();
      brief.close();
      councils.close();
      thread.close();
      runs.close();
    };
  });

  onMount(() => {
    projects.ensure().catch(() => undefined);
    models.ensure().catch(() => undefined);
    const clock = setInterval(() => (now = Date.now()), 10_000);
    // a heartbeat comes with no event: a session at work is read again, so its freshness is the host's
    const beat = setInterval(() => {
      if (runs.live) void runs.refresh().catch(() => undefined);
    }, 30_000);
    const stop = onLiveEvent((event) => {
      void thread.onEvent(event).catch(() => undefined);
      void cardPage.onEvent(event).catch(() => undefined);
      void brief.onEvent(event).catch(() => undefined);
      void councils.onEvent(event).catch(() => undefined);
      void runs.onEvent(event).catch(() => undefined);
    });
    return () => {
      clearInterval(clock);
      clearInterval(beat);
      stop();
    };
  });

  /** A decision was taken (or found changed) from the next step: the card, its decisions, thread and runs again. */
  function afterDecision() {
    void Promise.all([cardPage.reloadCard(), cardPage.reloadDecisions()])
      .then(() => thread.fetchNewer())
      .catch(() => undefined);
    void runs.refresh().catch(() => undefined);
  }

  const saveDescription = (text: string, revision: number) => cardPage.save({ description: text }, revision);
</script>

{#if cardPage.error?.status === 404 && cardPage.id === id}
  <div class="topbar"><BackButton /><h1>{t("error.not_found")}</h1></div>
{:else if !card && cardPage.error && cardPage.id === id}
  <p class="empty" role="alert">
    {errorText(cardPage.error)}
    <button type="button" class="btn small ghost" onclick={() => open(id)}>{t("app.retry")}</button>
  </p>
{:else if !card}
  <p class="empty">{t("app.loading")}</p>
{:else}
  <section class="cardview">
    <header class="card-h">
      <div class="title-row">
        <BackButton />
        <h1><span class="id">{card.id}</span> {card.title}</h1>
      </div>
      <dl class="props">
        <dt>{t("card.status")}</dt>
        <dd>
          {#if canTransition}
            <StatusOverride status={card.status} busy={cardPage.saving} onoverride={(status, reason) => void cardPage.override(status, reason)} />
          {:else}
            <StatusPill status={card.status} />
          {/if}
        </dd>
        <dt>{t("card.assignee")}</dt>
        <dd>
          {#if canEdit}
            <ChipMenu label={t("card.assignee")} value={card.assignee} options={assigneeOptions} disabled={cardPage.saving} onchange={(assignee) => void cardPage.save({ assignee })} />
          {:else}
            {card.assignee || t("card.unassigned")}
          {/if}
        </dd>
        <dt>{t("card.project")}</dt>
        <dd><ProjectChip id={card.project} /></dd>
        {#if canModel || card.model || card.effort}
          <ModelChips {card} editable={canModel} saving={cardPage.saving} onchange={(changes) => void cardPage.save(changes)} />
        {/if}
        {#if canModel || hasCouncil}
          <CouncilChip {card} editable={canModel} saving={cardPage.saving} bind:open={councilEditor} onchange={(changes) => void cardPage.save(changes)} />
        {/if}
        {#if briefView}
          <dt>{t("card.brief")}</dt>
          <dd>
            <button type="button" class="chip brief {briefTone(briefView.brief)}" onclick={() => show("brief")}>{briefLabel(briefView.brief)}</button>
          </dd>
        {/if}
        {#if more}
        {#if card.dependencies.length}
          <dt>{t("card.dependencies")}</dt>
          <dd>{#each card.dependencies as dep (dep)}<a class="chip" href="/cards/{dep}">{dep}</a>{/each}</dd>
        {/if}
        {#if card.parent}
          <dt>{t("card.parent")}</dt>
          <dd><a class="chip" href="/cards/{card.parent}">{card.parent}</a></dd>
        {/if}
        {#if card.due}
          <dt>{t("card.due")}</dt>
          <dd>{card.due}</dd>
        {/if}
        {#if card.priority}
          <dt>{t("card.priority")}</dt>
          <dd>{card.priority}</dd>
        {/if}
        {#if card.servers.length}
          <dt>{t("card.servers")}</dt>
          <dd>{#each card.servers as server (server)}<span class="chip info text">{server}</span>{/each}</dd>
        {/if}
        {#if card.labels.length}
          <dt>{t("card.labels")}</dt>
          <dd>{#each card.labels as label (label)}<span class="chip acc text">{label}</span>{/each}</dd>
        {/if}
        {#if counts && (counts.ac[1] || counts.dod[1])}
          <dt>{t("card.progress")}</dt>
          <dd class="num">
            {#if counts.ac[1]}<span>{t("card.chip.ac", { done: counts.ac[0], total: counts.ac[1] })}</span>{/if}
            {#if counts.dod[1]}<span>{t("card.chip.dod", { done: counts.dod[0], total: counts.dod[1] })}</span>{/if}
          </dd>
        {/if}
        {/if}
      </dl>
      <button type="button" class="btn small ghost more" aria-expanded={more} onclick={() => (more = !more)}>
        {t(more ? "card.less" : "card.more")}
      </button>
    </header>

    <div class="body">
    <aside class="side">
      <NextStep
        {card}
        actions={cardActions}
        decisions={cardPage.decisions}
        commands={cardRuns?.commands ?? []}
        sessions={cardRuns?.sessions ?? []}
        busy={cardPage.saving}
        {now}
        {canControl}
        onact={(action, comment, version) => void act(action, comment, version)}
        onchanged={afterDecision}
        oncontinue={continueSession}
      />
    </aside>
    <div class="content">
      <div class="tabs" role="tablist">
        {#each TABS as name (name)}
          <button type="button" role="tab" aria-selected={tab === name} class:on={tab === name} onclick={() => show(name)}>{t(tabLabel[name])}</button>
        {/each}
      </div>

    {#if tab === "thread"}
      <Thread
        cardId={card.id}
        decisions={cardPage.decisions}
        {canComment}
        agents={projects.agents}
        assignee={card.status === "Done" || card.status === "Cancelled" ? "" : card.assignee}
        {highlight}
        working={workingAgent(cardRuns?.sessions ?? [], now)}
        canOutput={canControl}
        councils={councils.of(card.id)}
        canManageCouncil={councils.canManage}
        councilBusy={councils.busy}
        ondecision={() => void cardPage.reloadDecisions().then(() => thread.fetchNewer())}
        oncouncilcancel={(council) => void cancelCouncil(council)}
        oncounciledit={canModel ? () => (councilEditor = true) : undefined}
      />
    {:else if tab === "brief"}
      <BriefTab
        view={briefView}
        {card}
        {canEdit}
        busy={cardPage.saving}
        draftKey={draftKey(session.me?.id ?? "", card.id, "description")}
        onsave={saveDescription}
        ontoggleac={(line) => void cardPage.toggleAcceptance(line)}
        onaddac={(text) => cardPage.addAcceptance(text)}
        ontoggledod={(line) => void cardPage.toggleDod(line)}
        onadddod={(text) => cardPage.addDod(text)}
      />
    {:else if tab === "result"}
      <div class="pane"><ResultTab cardId={card.id} /></div>
    {:else if tab === "history"}
      <div class="pane"><HistoryTab cardId={card.id} /></div>
    {:else}
      <div class="pane">
        <Sessions sessions={cardRuns?.sessions ?? []} jobs={cardRuns?.jobs ?? []} {canControl} onchanged={() => void runs.refresh().catch(() => undefined)} />
      </div>
    {/if}
    </div>
    </div>
  </section>
{/if}

<style>
  .cardview {
    --screen-h: calc(100dvh - var(--nav-h) - env(safe-area-inset-bottom, 0px) - env(safe-area-inset-top, 0px) - 16px);
    display: flex; flex-direction: column; height: var(--screen-h);
  }
  @media (min-width: 900px) { .cardview { --screen-h: calc(100dvh - 32px); } }
  .card-h { display: flex; flex-direction: column; gap: 8px; padding: 14px 0 8px; flex: none; }
  .card-h h1 { font-size: 20px; margin: 0; font-weight: 700; line-height: 1.3; text-wrap: balance; overflow-wrap: anywhere; }
  .card-h .more { align-self: flex-start; }
  .title-row { display: flex; align-items: flex-start; gap: 10px; min-width: 0; }
  .title-row h1 { flex: 1; min-width: 0; }
  /* the next step above the tabs on a phone (a compact block, the field below keeps its place), beside them on a desktop */
  .body { flex: 1; min-height: 0; display: flex; flex-direction: column; gap: 10px; }
  .side { flex: none; }
  .content { flex: 1; min-height: 0; min-width: 0; display: flex; flex-direction: column; }
  /* the next step above the tabs: the header and the next step scroll away with the page, the tabs,
     the thread and its field keep a whole screen — fixed above it they left the thread a line */
  @media (max-width: 1099px) {
    .cardview { height: auto; }
    .body { flex: none; }
    .content { flex: none; height: var(--screen-h); }
  }
  @media (min-width: 1100px) {
    .body { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 16px; }
    .side { order: 2; overflow-y: auto; }
    .content { order: 1; }
  }
  .card-h .id { color: var(--muted); font-weight: 500; }
  /* the card's facts as label → value rows (two pairs per row on a desktop) */
  .props { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 6px 14px; margin: 0; align-items: center; font-size: 14px; }
  .props dt { margin: 0; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); }
  .props dd { margin: 0; min-width: 0; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .props a.chip { text-decoration: none; color: var(--accent); }
  @media (min-width: 900px) { .props { grid-template-columns: max-content minmax(0, 1fr) max-content minmax(0, 1fr); } }
  .pane { flex: 1; min-height: 0; overflow-y: auto; padding: 14px 2px; }
  .props .chip.brief { cursor: pointer; font: inherit; }
</style>
