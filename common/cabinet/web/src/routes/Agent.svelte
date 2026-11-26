<script lang="ts">
  // One agent: its state from facts and what it is made of (questions, failures in words, cards
  // by status, heartbeat and last action), specializations, today's spend, its latest progress lines and its
  // cards; an agent on the session host also its slots, its chat (agents.chat) and its sessions.
  import { onMount } from "svelte";
  import AgentFacts from "../components/AgentFacts.svelte";
  import Avatar from "../components/Avatar.svelte";
  import BackButton from "../components/BackButton.svelte";
  import Badge from "../components/Badge.svelte";
  import CardRow from "../components/CardRow.svelte";
  import Freshness from "../components/Freshness.svelte";
  import Markdown from "../components/Markdown.svelte";
  import PushBanner from "../components/PushBanner.svelte";
  import Sessions from "../components/Sessions.svelte";
  import Thread from "../components/Thread.svelte";
  import { ApiError, apiGet } from "../lib/api";
  import { namedCards, statusLine } from "../lib/agentline";
  import { chats, readAll } from "../lib/chats.svelte";
  import { navigate, router } from "../lib/router.svelte";
  import { SPACES, spaceKey, type Space } from "../lib/spaces";
  import { isToolMessage } from "../lib/events";
  import { coalesce } from "../lib/coalesce";
  import { errorText, t } from "../lib/i18n.svelte";
  import { onLiveEvent, watchAll } from "../lib/live.svelte";
  import { session } from "../lib/session.svelte";
  import { thread } from "../lib/stores/thread.svelte";
  import { synced } from "../lib/synced.svelte";
  import { STATUSES, type AgentDetail } from "../lib/types";
  import { age, when } from "../lib/time";
  import { toast } from "../lib/toast.svelte";

  let { name }: { name: string } = $props();

  const COORDINATOR = "boss"; // the platform's one coordinator (worker_board.COORDINATOR)

  const order = (status: string) => (STATUSES as readonly string[]).indexOf(status);
  let agent = $state<AgentDetail | null>(null);
  let missing = $state(false);
  const cards = $derived(
    agent ? [...agent.cards].sort((a, b) => order(a.status) - order(b.status) || b.n - a.n) : [],
  );
  const hosted = $derived(!!agent?.host);
  const canChat = $derived(session.can("agents.chat"));
  // the coordinator's page is a chat first; facts, sessions and spend wait under "More → Diagnostics"
  const calm = $derived(!!agent && agent.name === COORDINATOR && hosted && canChat);
  let panel = $state<"" | "tasks" | "diagnostics">("");
  let menu = $state(false);
  const toggle = (which: "tasks" | "diagnostics") => (panel = panel === which ? "" : which);
  // "Tasks": live cards of the agent and those the chat names, not every card it ever had (closed probes stay out)
  const active = $derived(cards.filter((card) => card.status !== "Done" && card.status !== "Cancelled"));
  const mentioned = $derived.by(() => {
    if (!calm) return [];
    const own = new Set(active.map((card) => card.id));
    const loaded = space === "chat" && thread.agent === spaceOpen ? thread.messages : [];
    const bodies = loaded.filter((m) => m.kind === "text").map((m) => m.body);
    return namedCards(bodies, session.me?.cards).filter((id) => !own.has(id)).slice(-20);
  });

  /** "Mark the chat as read": what the page holds now, not what comes later. */
  function readEverything() {
    menu = false;
    const last = thread.messages.at(-1)?.seq ?? 0;
    if (thread.agent === spaceOpen && last) readAll(spaceOpen, last); // the thread on the page
  }
  let chatOpen = false;
  let closed = false; // the page has left: a read on its way changes nothing, the shared thread least

  // An agent with a "Monitor" thread shows it as a tab beside its chat; the tab is in the address
  // (`?tab=monitor` — a push of a "Monitor" reply opens it); the coordinator's "Monitor" is its work log
  // (attention ticks), a tab of its calm page too
  const asked = $derived<Space>(router.current.query.get("tab") === "monitor" ? "monitor" : "chat");
  const space = $derived<Space>(agent?.monitor ? asked : "chat");
  const spaceOpen = $derived(spaceKey(name, space));
  function showSpace(next: Space) {
    const base = `/agents/${encodeURIComponent(name)}`;
    navigate(next === "chat" ? base : `${base}?tab=${next}`, { replace: true });
  }
  $effect(() => {
    const key = spaceOpen;
    if (chatOpen && thread.agent !== key) void thread.loadAgent(key); // the tab moved: the other thread opens
  });

  const data = synced(
    async () => {
      const found = await apiGet<AgentDetail>(`/api/agents/${encodeURIComponent(name)}`);
      if (closed) return;
      agent = found;
      if (agent.host && canChat && !chatOpen) {
        chatOpen = true;
        void thread.loadAgent(spaceOpen); // a failure is the thread's error (shown with «retry») and retried
      }
    },
    {
      onError: (exc) => {
        if (exc instanceof ApiError && exc.status === 404) missing = true;
        else toast(errorText(exc), "error");
      },
    },
  );
  const load = () => data.run();
  const refresh = coalesce(load);

  onMount(() => {
    void load();
    const release = watchAll();
    const stop = onLiveEvent((event) => {
      void thread.onEvent(event).catch(() => undefined);
      if (!isToolMessage(event)) refresh(); // a turn's tool calls change no fact here
    });
    const tick = setInterval(load, 30_000); // a session that stopped beating sends no event (Review Focus 4)
    return () => {
      closed = true;
      refresh.cancel();
      clearInterval(tick);
      stop();
      release();
      data.dispose();
      if (chatOpen) thread.close();
    };
  });
</script>

{#snippet spaceTabs(who: string)}
  <div class="tabs" role="tablist">
    {#each SPACES as one (one)}
      <button type="button" role="tab" aria-selected={space === one} class:on={space === one} onclick={() => showSpace(one)}>
        {t(one === "chat" ? "agent.chat" : "agent.monitor")}
        <Badge count={chats.unread(spaceKey(who, one))} label={t("nav.unread", { name: t(one === "chat" ? "agent.chat" : "agent.monitor"), count: chats.unread(spaceKey(who, one)) })} />
      </button>
    {/each}
  </div>
{/snippet}

{#if missing}
  <div class="topbar"><h1>{t("error.not_found")}</h1></div>
{:else if !agent}
  <Freshness {data} />
  {#if !data.error}<p class="empty">{t("app.loading")}</p>{/if}
{:else if calm}
  <!-- the coordinator's page is its chat; the technical rest opens on request -->
  {@const line = statusLine(agent.state, agent.facts, agent.name)}
  <div class="calm">
    <header class="chat-head">
      <Avatar name={agent.name} kind="boss" size={32} />
      <div class="who">
        <h1>{agent.name.toUpperCase()}</h1>
        <span class="line {line.tone}" role="status">{t(line.key, line.params)}</span>
      </div>
      <button type="button" class="btn small ghost" aria-expanded={panel === "tasks"} onclick={() => toggle("tasks")}>
        {t("agent.tasks")}
      </button>
      <div class="more">
        <button type="button" class="btn small ghost" aria-haspopup="menu" aria-expanded={menu} onclick={() => (menu = !menu)}>
          {t("agent.more")}
        </button>
        {#if menu}
          <div class="menu" role="menu">
            <button type="button" role="menuitem" onclick={() => { menu = false; toggle("diagnostics"); }}>{t("agent.diagnostics")}</button>
            <button type="button" role="menuitem" disabled={!thread.messages.length} onclick={readEverything}>{t("agent.read_all")}</button>
          </div>
        {/if}
      </div>
    </header>
    {#if agent.facts.questions.length}
      <a class="needs" href="/inbox">{t("agent.needs_you", { count: agent.facts.questions.length })} →</a>
    {/if}
    {#if panel === "tasks"}
      <section class="panel" aria-label={t("agent.tasks")}>
        {#if active.length === 0 && mentioned.length === 0}
          <p class="empty small">{t("agent.no_tasks")}</p>
        {/if}
        {#each active as card (card.id)}
          <div class="task"><CardRow {card} status /><span class="why">{t("agent.task.assigned")}</span></div>
        {/each}
        {#each mentioned as id (id)}
          <div class="task"><a href="/cards/{id}">{id}</a><span class="why">{t("agent.task.mentioned")}</span></div>
        {/each}
      </section>
    {:else if panel === "diagnostics"}
      <section class="panel diag" aria-label={t("agent.diagnostics")}>
        <p class="hint">{t("agent.diag.facts")}</p>
        <AgentFacts facts={agent.facts} cards={agent.counts} hosted={hosted} failures />
        <div class="facts">
          {#if agent.usage_today}
            <span class="chip">{t("agents.today", { cost: `$${agent.usage_today.cost.toFixed(2)}`, calls: agent.usage_today.calls })}</span>
          {/if}
          {#if agent.host}<span class="chip info">{t("agents.slots", { ...agent.host })}</span>{/if}
        </div>
        <p class="hint">{t("agent.diag.sessions")}</p>
        <Sessions sessions={agent.sessions} canControl={session.can("agents.control")} showAgent={false} onchanged={load} />
      </section>
    {/if}
    <PushBanner name={agent.name.toUpperCase()} />
    {#if agent.monitor}{@render spaceTabs(agent.name)}{/if}
    <div class="chat full">
      {#key spaceOpen}
        <Thread chatAgent={spaceOpen} quiet working={agent.state === "working" ? `@${agent.name}` : ""}
          decisions={{}} canComment={canChat} agents={[]} ondecision={() => undefined} />
      {/key}
    </div>
  </div>
{:else}
  <div class="topbar">
    <a class="back" href="/agents" aria-label={t("nav.agents")}>←</a>
    <BackButton />
    <Avatar name={agent.name} kind={agent.name === "boss" ? "boss" : "agent"} size={32} />
    <h1>@{agent.name}</h1>
    <span class="chip state {agent.state}">{t(`agents.state.${agent.state}`)}</span>
    <Freshness {data} />
  </div>

  <div class="state-facts">
    <AgentFacts facts={agent.facts} cards={agent.counts} hosted={hosted} failures />
  </div>

  <div class="facts">
    {#if agent.usage_today}
      <span class="chip">{t("agents.today", { cost: `$${agent.usage_today.cost.toFixed(2)}`, calls: agent.usage_today.calls })}</span>
    {/if}
    {#each agent.specs as spec (spec)}<span class="chip acc">{spec}</span>{/each}
    {#if agent.host}
      <span class="chip info">{t("agents.slots", { ...agent.host })}</span>
    {/if}
  </div>

  {#if hosted && canChat}
    {#if agent.monitor}
      {@render spaceTabs(agent.name)}
    {:else}
      <h2 class="section-h">{t("agent.chat")}</h2>
    {/if}
    <div class="chat">
      {#key spaceOpen}
        <Thread chatAgent={spaceOpen} decisions={{}} canComment={canChat} agents={[]} ondecision={() => undefined} />
      {/key}
    </div>
  {/if}

  <h2 class="section-h">{t("agent.progress")}</h2>
  {#if agent.progress.length === 0}
    <p class="empty small">{t("agent.no_progress")}</p>
  {:else}
    <ol class="progress">
      {#each agent.progress as line (`${line.card}:${line.seq}`)}
        <li>
          <span class="meta"><a href="/cards/{line.card}">{line.card}</a> · <span title={when(line.created)}>{age(line.created)}</span></span>
          <Markdown text={line.body} inline />
        </li>
      {/each}
    </ol>
  {/if}

  <h2 class="section-h">{t("agent.cards")} <span class="n">{cards.length}</span></h2>
  {#if cards.length === 0}
    <p class="empty small">{t("board.empty")}</p>
  {:else}
    <div class="list">{#each cards as card (card.id)}<CardRow {card} status />{/each}</div>
  {/if}

  {#if hosted}
    <h2 class="section-h">{t("agent.sessions")}</h2>
    <Sessions sessions={agent.sessions} canControl={session.can("agents.control")} showAgent={false} onchanged={load} />
  {/if}
{/if}

<style>
  .back { color: var(--muted); text-decoration: none; font-size: var(--fs-heading); }
  @media (min-width: 900px) { .back { display: none; } } /* «Назад» there */
  .facts { display: flex; flex-wrap: wrap; gap: var(--sp-6); margin-bottom: var(--sp-6); }
  .state-facts { display: grid; gap: var(--sp-6); margin-bottom: var(--sp-10); }
  .state.working, .state.free { color: var(--good); background: var(--good-bg); }
  .state.waiting_you { color: var(--accent); }
  .state.queued { color: var(--warn); }
  .state.error, .state.unavailable { color: var(--crit); background: var(--crit-bg); }
  .progress { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--sp-8); }
  .progress li { background: var(--surface); border: 1px solid var(--line-soft); border-radius: 10px; padding: var(--sp-8) var(--sp-12); display: grid; gap: var(--sp-4); font-size: var(--fs-ui); }
  .meta { font-size: var(--fs-sm); color: var(--muted); }
  .meta a { color: var(--accent); text-decoration: none; }
  .list { display: flex; flex-direction: column; gap: var(--sp-6); }
  .small { padding: var(--sp-8) 0; }
  .chat { display: flex; flex-direction: column; height: min(60dvh, 560px); }
  /* the coordinator's chat takes the screen; one scroller (the thread), the field stays in view */
  .calm { flex: 1; min-height: 0; display: flex; flex-direction: column; gap: var(--sp-8); }
  .chat-head { flex: none; display: flex; align-items: center; gap: var(--sp-10); padding: var(--sp-10) 0 var(--sp-6); border-bottom: 1px solid var(--line-soft); }
  .chat-head .who { flex: 1; min-width: 0; display: grid; }
  .chat-head h1 { margin: 0; font-size: var(--fs-title); }
  .line { font-size: var(--fs-md); color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .line.busy { color: var(--accent); }
  .line.attention { color: var(--warn); }
  .line.bad { color: var(--crit); }
  .more { position: relative; }
  .menu { position: absolute; right: 0; top: calc(100% + 4px); z-index: 10; display: grid; min-width: 220px; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; box-shadow: 0 4px 16px rgb(0 0 0 / 0.3); overflow: hidden; }
  .menu button { background: none; border: 0; text-align: left; padding: var(--sp-10) var(--sp-14); color: var(--text); cursor: pointer; }
  .menu button:hover { background: var(--surface-2); }
  .needs { flex: none; color: var(--accent); text-decoration: none; font-size: var(--fs-ui); padding: var(--sp-6) var(--sp-10); border: 1px solid var(--accent); border-radius: var(--r-md); }
  .panel { flex: none; max-height: 40dvh; overflow-y: auto; display: grid; gap: var(--sp-8); padding: var(--sp-8) 0; border-bottom: 1px solid var(--line-soft); }
  .task { display: grid; gap: var(--sp-2); }
  .task a { color: var(--accent); text-decoration: none; }
  .why, .hint { font-size: var(--fs-sm); color: var(--muted); margin: 0; }
  .chat.full { flex: 1; min-height: 0; height: auto; }
  .tabs { display: flex; gap: var(--sp-4); margin: var(--sp-12) 0 var(--sp-6); border-bottom: 1px solid var(--line-soft); }
  .tabs button { background: none; border: 0; border-bottom: 2px solid transparent; padding: var(--sp-6) var(--sp-12); color: var(--muted); cursor: pointer; font-size: var(--fs-body); display: inline-flex; gap: var(--sp-6); align-items: center; }
  .tabs button.on { color: var(--text); border-bottom-color: var(--accent); }
  .calm .tabs { flex: none; margin: 0; }
</style>
