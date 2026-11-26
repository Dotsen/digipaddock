<script lang="ts">
  // The app's menu: a bottom tab bar on a phone, a side menu from 900px. The inbox first, then the dashboard
  // and the coordinator's page with its chat (new tasks and "how is it going" in one tap); the inbox
  // shows its badge. The tab bar keeps the inbox, the dashboard, BOSS and the deploys (the agents for who does
  // not read the deploys), the rest is under "More" (a sheet from below); the side menu folds to a strip of icons. The incidents join the tab bar
  // for who reads the servers, with the count of those not resolved. At the top of the side menu (the first row of
  // "More" on a phone) the cards' search and "+": the search opens the board searched — or the card itself when a
  // number names one card; "+" opens the new card's form from any page. Folded, both are icons; the search's
  // unfolds the menu on its field.
  import { tick } from "svelte";
  import { searchTarget } from "../lib/findcard";
  import { t, type Key } from "../lib/i18n.svelte";
  import { chats } from "../lib/chats.svelte";
  import { live } from "../lib/live.svelte";
  import { newCardForm } from "../lib/newcardform.svelte";
  import { navigate, router, type RouteName } from "../lib/router.svelte";
  import { session } from "../lib/session.svelte";
  import { sideMenu } from "../lib/sidemenu.svelte";
  import Badge from "./Badge.svelte";

  interface Item {
    path: string;
    label: Key;
    routes: RouteName[];
    icon: string;
    agent?: string; // the agent route of this agent only; the agents item then skips it
    more?: boolean; // on a phone: under "More", not in the tab bar
    moreFor?: string; // under "More" for who holds this permission (its tab goes to the item that needs it)
    perm?: string; // shown only to who holds it (the servers — servers.read)
  }

  // The platform's one coordinator (worker_board.COORDINATOR).
  const COORDINATOR = "boss";

  // The side menu's order; a new section under "More" is one more entry with `more: true`.
  const ALL: Item[] = [
    {
      path: "/inbox",
      label: "nav.inbox",
      routes: ["inbox", "decision"],
      icon: '<path d="M3 13l3-8h12l3 8v6H3z"/><path d="M3 13h5l2 3h4l2-3h5"/>',
    },
    {
      path: "/",
      label: "nav.dashboard",
      routes: ["dashboard"],
      icon: '<rect x="3" y="3" width="8" height="8" rx="1"/><rect x="13" y="3" width="8" height="5" rx="1"/><rect x="13" y="10" width="8" height="11" rx="1"/><rect x="3" y="13" width="8" height="8" rx="1"/>',
    },
    {
      path: `/agents/${COORDINATOR}`,
      label: "nav.boss",
      routes: ["agent"],
      agent: COORDINATOR,
      icon: '<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9h8M8 12h5"/>',
    },
    {
      path: "/board",
      label: "nav.board",
      routes: ["board", "card"],
      more: true,
      icon: '<rect x="3" y="4" width="5" height="16" rx="1"/><rect x="10" y="4" width="5" height="10" rx="1"/><rect x="17" y="4" width="4" height="13" rx="1"/>',
    },
    {
      // the projects' deploys and their journal: the owner and who holds deploys.read; the agents' tab
      path: "/deploys",
      label: "nav.deploys",
      routes: ["deploys"],
      perm: "deploys.read",
      icon: '<path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M4 17v3h16v-3"/>',
    },
    {
      path: "/servers",
      label: "nav.servers",
      routes: ["servers", "server", "monitor-settings"],
      more: true,
      perm: "servers.read",
      icon: '<rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01M11 7.5h6M11 16.5h6"/>',
    },
    {
      // in the phone's tab bar, with the count of incidents not resolved
      path: "/incidents", label: "nav.incidents", routes: ["incidents", "incident"], perm: "servers.read",
      icon: '<path d="M12 3L2 21h20L12 3z"/><path d="M12 9v5M12 17v1"/>',
    },
    {
      // in the tab bar for who does not read the deploys, under "More" for who does
      path: "/agents",
      label: "nav.agents",
      routes: ["agents", "agent"],
      moreFor: "deploys.read",
      icon: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/>',
    },
    {
      path: "/feed",
      label: "nav.feed",
      routes: ["feed"],
      more: true,
      icon: '<path d="M4 6h16M4 12h16M4 18h10"/>',
    },
    {
      path: "/settings",
      label: "nav.settings",
      routes: ["settings", "users", "projects", "hindsight"],
      more: true,
      icon: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2"/>',
    },
  ];

  const MORE_ICON = '<circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/>';
  const FOLD_ICON = '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>';
  const LOGOUT_ICON = '<path d="M15 4h4v16h-4"/><path d="M10 8l-4 4 4 4M6 12h10"/>';
  const ADD_ICON = '<path d="M12 5v14M5 12h14"/>';
  const FIND_ICON = '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/>';

  // The chat item is for those who may write to agents; a viewer keeps the agents page. An item with a permission
  // is for its holders only (the servers: a partner never sees them).
  const ITEMS = $derived(
    ALL.filter((item) => (!item.agent || session.can("agents.chat")) && (!item.perm || session.can(item.perm))),
  );
  const underMore = (item: Item) => item.more || (!!item.moreFor && session.can(item.moreFor));
  const TABS = $derived(ITEMS.filter((item) => !underMore(item)));
  const MORE = $derived(ITEMS.filter(underMore));
  const coordinatorItem = $derived(ITEMS.some((item) => item.agent === COORDINATOR));
  const moreActive = $derived(MORE.some(active));

  const canAdd = $derived(session.canAny("cards.create"));

  let moreOpen = $state(false);
  let side: HTMLElement;
  let findText = $state("");
  let finding = false;

  async function find(event: SubmitEvent) {
    event.preventDefault();
    if (finding) return;
    finding = true;
    try {
      const path = await searchTarget(findText);
      moreOpen = false;
      findText = "";
      navigate(path);
    } finally {
      finding = false;
    }
  }

  /** Folded: the search's icon unfolds the menu on its field. */
  async function unfoldToFind() {
    sideMenu.toggle();
    await tick();
    side.querySelector<HTMLInputElement>(".find input")?.focus();
  }

  /** The new card's form; on the board it opens on the board's project. */
  function add() {
    moreOpen = false;
    const route = router.current;
    newCardForm.show(route.name === "board" ? (route.query.get("project") ?? "") : "");
  }

  function active(item: Item): boolean {
    const route = router.current;
    if (!item.routes.includes(route.name)) return false;
    if (route.name !== "agent") return true;
    if (item.agent) return route.params.name === item.agent;
    return !(coordinatorItem && route.params.name === COORDINATOR);
  }

  // the coordinator's item counts its new replies, apart from the inbox's decisions
  function unreadLabel(item: Item): string {
    return t("nav.unread", { name: t(item.label), count: chats.total(item.agent ?? "") });
  }

  function incidentsLabel(): string {
    return t("nav.incidents_open", { count: live.incidents });
  }

  function go(event: MouseEvent, item: Item) {
    event.preventDefault();
    moreOpen = false;
    navigate(item.path);
  }

  function onKey(event: KeyboardEvent) {
    if (moreOpen && event.key === "Escape") moreOpen = false;
  }
</script>

<svelte:window onkeydown={onKey} />

{#snippet finder()}
  <form class="find" role="search" onsubmit={find}>
    <svg class="glass" viewBox="0 0 24 24" aria-hidden="true">{@html FIND_ICON}</svg>
    <input type="search" placeholder={t("nav.find")} aria-label={t("nav.find")} enterkeyhint="search" bind:value={findText} />
    {#if canAdd}
      <button type="button" class="plus" aria-haspopup="dialog" aria-label={t("nav.add_card")} title={t("nav.add_card")} onclick={add}>
        <svg viewBox="0 0 24 24" aria-hidden="true">{@html ADD_ICON}</svg>
      </button>
    {/if}
  </form>
{/snippet}

<nav class="side" class:folded={sideMenu.collapsed} aria-label={t("app.title")} bind:this={side}>
  <div class="head">
    {#if !sideMenu.collapsed}<img class="brand" src="/images/logo.png" alt={t("app.title")} width="110" height="36" />{/if}
    <button
      type="button"
      class="fold"
      aria-label={t(sideMenu.collapsed ? "nav.expand" : "nav.collapse")}
      title={t(sideMenu.collapsed ? "nav.expand" : "nav.collapse")}
      aria-expanded={!sideMenu.collapsed}
      onclick={() => sideMenu.toggle()}
    >
      <svg viewBox="0 0 24 24" aria-hidden="true">{@html FOLD_ICON}</svg>
    </button>
  </div>
  {#if sideMenu.collapsed}
    <button type="button" class="plus" aria-label={t("nav.find")} title={t("nav.find")} onclick={unfoldToFind}>
      <svg viewBox="0 0 24 24" aria-hidden="true">{@html FIND_ICON}</svg>
    </button>
    {#if canAdd}
      <button type="button" class="plus" aria-haspopup="dialog" aria-label={t("nav.add_card")} title={t("nav.add_card")} onclick={add}>
        <svg viewBox="0 0 24 24" aria-hidden="true">{@html ADD_ICON}</svg>
      </button>
    {/if}
  {:else}
    {@render finder()}
  {/if}
  {#each ITEMS as item (item.path)}
    <a
      href={item.path}
      class:on={active(item)}
      aria-current={active(item) ? "page" : undefined}
      aria-label={sideMenu.collapsed ? t(item.label) : undefined}
      title={sideMenu.collapsed ? t(item.label) : undefined}
      onclick={(e) => go(e, item)}
    >
      <svg viewBox="0 0 24 24" aria-hidden="true">{@html item.icon}</svg>
      {#if !sideMenu.collapsed}<span>{t(item.label)}</span>{/if}
      {#if item.path === "/inbox"}<span class="badge-at"><Badge count={live.inbox} /></span>{/if}
      {#if item.agent}<span class="badge-at"><Badge count={chats.total(item.agent)} label={unreadLabel(item)} /></span>{/if}
      {#if item.path === "/incidents"}<span class="badge-at"><Badge count={live.incidents} label={incidentsLabel()} /></span>{/if}
    </a>
  {/each}
  <div class="who">
    {#if sideMenu.collapsed}
      <button class="fold" type="button" aria-label={t("action.logout")} title={t("action.logout")} onclick={() => session.logout()}>
        <svg viewBox="0 0 24 24" aria-hidden="true">{@html LOGOUT_ICON}</svg>
      </button>
    {:else}
      <span class="muted">{session.me?.name}</span>
      <button class="btn" type="button" onclick={() => session.logout()}>{t("action.logout")}</button>
    {/if}
  </div>
</nav>

<nav class="tabbar" aria-label={t("app.title")}>
  {#each TABS as item (item.path)}
    <a href={item.path} class:on={active(item)} aria-current={active(item) ? "page" : undefined} onclick={(e) => go(e, item)}>
      <svg viewBox="0 0 24 24" aria-hidden="true">{@html item.icon}</svg>
      <span>{t(item.label)}</span>
      {#if item.path === "/inbox"}<span class="dot"><Badge count={live.inbox} /></span>{/if}
      {#if item.agent}<span class="dot"><Badge count={chats.total(item.agent)} label={unreadLabel(item)} /></span>{/if}
      {#if item.path === "/incidents"}<span class="dot"><Badge count={live.incidents} label={incidentsLabel()} /></span>{/if}
    </a>
  {/each}
  <button
    type="button"
    class="tab"
    class:on={moreActive}
    aria-current={moreActive ? "page" : undefined}
    aria-haspopup="dialog"
    aria-expanded={moreOpen}
    onclick={() => (moreOpen = !moreOpen)}
  >
    <svg viewBox="0 0 24 24" aria-hidden="true">{@html MORE_ICON}</svg>
    <span>{t("nav.more")}</span>
  </button>
</nav>

{#if moreOpen}
  <div class="veil more-veil" role="presentation" onclick={(e) => e.target === e.currentTarget && (moreOpen = false)}>
    <div class="sheet more-sheet" role="dialog" aria-modal="true" aria-label={t("nav.more")}>
      {@render finder()}
      {#each MORE as item (item.path)}
        <a href={item.path} class:on={active(item)} aria-current={active(item) ? "page" : undefined} onclick={(e) => go(e, item)}>
          <svg viewBox="0 0 24 24" aria-hidden="true">{@html item.icon}</svg>
          <span>{t(item.label)}</span>
        </a>
      {/each}
    </div>
  </div>
{/if}

<style>
  .side { display: none; }
  .tabbar {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
    height: calc(var(--nav-h) + env(safe-area-inset-bottom, 0px));
    padding: 0 var(--sp-8) env(safe-area-inset-bottom, 0px);
    display: grid; grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr);
    background: var(--surface); border-top: 1px solid var(--line);
  }
  .tabbar a, .tabbar .tab { font-size: var(--fs-tiny); }
  .tabbar span:not(.dot) { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  a, .tab {
    display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 3px;
    color: var(--muted); font-size: var(--fs-xs); position: relative; text-decoration: none;
  }
  .tab { background: none; border: 0; padding: 0; }
  a.on, .tab.on { color: var(--text); }
  svg {
    width: 22px; height: 22px; fill: none; stroke: currentColor; stroke-width: 1.8;
    stroke-linecap: round; stroke-linejoin: round;
  }
  .dot { position: absolute; top: 6px; left: calc(50% + 6px); }
  /* The sheet ends above the tab bar; its height follows the visible entries. */
  .more-veil { bottom: calc(var(--nav-h) + env(safe-area-inset-bottom, 0px)); }
  .more-sheet { gap: var(--sp-4); padding: var(--sp-8); max-height: calc(100dvh - var(--nav-h) - env(safe-area-inset-bottom, 0px) - 16px); align-content: start; }
  .more-sheet a { flex-direction: row; justify-content: flex-start; gap: var(--sp-12); padding: var(--sp-12); border-radius: var(--r-md); font-size: var(--fs-body); }
  .more-sheet a.on { background: var(--surface-2); }
  /* the search and "+" in one row: the field takes the width, "+" a square beside it */
  .find { display: flex; gap: var(--sp-8); align-items: center; position: relative; min-width: 0; }
  .find input { flex: 1; min-width: 0; padding-left: 34px; }
  .find .glass { position: absolute; left: 10px; width: 17px; height: 17px; color: var(--muted); pointer-events: none; }
  .plus {
    flex: none; width: 44px; height: 44px; display: grid; place-items: center; padding: 0;
    border: 1px solid var(--line); border-radius: var(--r-md); background: none; color: var(--accent); cursor: pointer;
  }
  .plus:hover { background: var(--surface-2); }
  .more-sheet .find { margin-bottom: var(--sp-4); }
  .fold {
    background: none; border: 0; padding: var(--sp-6); border-radius: var(--r-md); color: var(--muted);
    display: grid; place-items: center;
  }
  .fold:hover { color: var(--text); background: var(--surface-2); }
  .fold svg { width: 20px; height: 20px; }
  @media (min-width: 900px) {
    .tabbar, .more-veil { display: none; }
    .side {
      display: flex; flex-direction: column; gap: var(--sp-4); padding: var(--sp-20) var(--sp-12); position: sticky; top: 0;
      height: 100vh; border-right: 1px solid var(--line); background: var(--surface); overflow-y: auto;
    }
    .head { display: flex; align-items: center; gap: var(--sp-6); padding: 0 0 var(--sp-12) var(--sp-12); }
    .brand { display: block; width: 110px; height: auto; flex: 0 1 110px; min-width: 0; margin-right: auto; object-fit: contain; }
    .side a { flex-direction: row; justify-content: flex-start; gap: var(--sp-10); padding: 9px var(--sp-12); border-radius: var(--r-md); font-size: var(--fs-ui); }
    .side a.on { background: var(--surface-2); }
    .side .find { margin-bottom: var(--sp-8); }
    .side .find input { padding-top: 7px; padding-bottom: 7px; }
    .side .plus { width: 38px; height: 38px; }
    .side.folded .plus { margin-bottom: var(--sp-4); }
    .side .plus svg { width: 20px; height: 20px; }
    .side .badge-at { margin-left: auto; }
    .who { margin-top: auto; display: grid; gap: var(--sp-8); padding: 0 var(--sp-12); font-size: var(--fs-md); }
    /* folded: a strip of icons, the badges over them, the names in aria-label and the tooltip */
    .side.folded { padding: var(--sp-16) var(--sp-8); align-items: center; }
    .side.folded .head { padding: 0 0 var(--sp-12); }
    .side.folded a { padding: 9px; justify-content: center; }
    .side.folded .badge-at { position: absolute; top: 0; right: -4px; margin: 0; }
    .side.folded .who { padding: 0; justify-items: center; }
  }
</style>
