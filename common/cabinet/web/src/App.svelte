<script lang="ts" module>
  import { CANON, canonicalCard } from "./lib/names";
  import type { Route } from "./lib/router.svelte";
  import type { Me } from "./lib/session.svelte";

  function nextPath(raw: string | null): string {
    return raw && raw.startsWith("/") && !raw.startsWith("//") && !raw.startsWith("/login") ? raw : "/";
  }

  /** Where the shell sends this person from this route, or null to stay. The setup page is offered right after
   * sign-in only, so a deep link or a reload never lands elsewhere; a banner reminds about it meanwhile. */
  export function landing(me: Me | null, route: Route): string | null {
    if (!me) {
      if (route.name === "login") return null;
      return route.name === "dashboard" || route.name === "notfound" ? "/login" : `/login?next=${encodeURIComponent(route.path)}`;
    }
    if (route.name === "card") {
      // an old link names the card by an earlier prefix of the host: the page is the current ID's
      const names = { ...CANON, cards: me.cards || CANON.cards, aliases: me.card_aliases };
      const id = canonicalCard(route.params.id, names);
      const query = route.query.toString();
      return id === route.params.id ? null : `/cards/${encodeURIComponent(id)}${query ? `?${query}` : ""}`;
    }
    if (route.name !== "login") return null;
    return me.must_setup ? "/setup" : nextPath(route.query.get("next"));
  }
</script>

<script lang="ts">
  // The shell: who is signed in decides between the sign-in page and the app; the router picks the screen.
  import { onMount } from "svelte";
  import Nav from "./components/Nav.svelte";
  import NewCard from "./components/NewCard.svelte";
  import Toast from "./components/Toast.svelte";
  import { errorText, t } from "./lib/i18n.svelte";
  import { live, startLive, stopLive } from "./lib/live.svelte";
  import { showUnread } from "./lib/marks";
  import { newCardForm } from "./lib/newcardform.svelte";
  import { navigate, router, startRouter } from "./lib/router.svelte";
  import { session } from "./lib/session.svelte";
  import { sideMenu } from "./lib/sidemenu.svelte";
  import { feed } from "./lib/stores/feed.svelte";
  import { models } from "./lib/stores/models.svelte";
  import { overview } from "./lib/stores/overview.svelte";
  import { projects } from "./lib/stores/projects.svelte";
  import { retryStale, staleness } from "./lib/synced.svelte";
  import { when } from "./lib/time";
  import { toast } from "./lib/toast.svelte";
  import StepUp from "./components/StepUp.svelte";
  import Agent from "./routes/Agent.svelte";
  import Agents from "./routes/Agents.svelte";
  import Board from "./routes/Board.svelte";
  import Card from "./routes/Card.svelte";
  import Dashboard from "./routes/Dashboard.svelte";
  import Decision from "./routes/Decision.svelte";
  import Feed from "./routes/Feed.svelte";
  import SettingsHindsight from "./routes/SettingsHindsight.svelte";
  import Inbox from "./routes/Inbox.svelte";
  import Incident from "./routes/Incident.svelte";
  import Incidents from "./routes/Incidents.svelte";
  import Login from "./routes/Login.svelte";
  import MonitorSettings from "./routes/MonitorSettings.svelte";
  import Server from "./routes/Server.svelte";
  import "./monitoring.css";
  import Servers from "./routes/Servers.svelte";
  import Settings from "./routes/Settings.svelte";
  import SettingsModels from "./routes/SettingsModels.svelte";
  import SettingsProjects from "./routes/SettingsProjects.svelte";
  import SettingsUsers from "./routes/SettingsUsers.svelte";
  import Setup from "./routes/Setup.svelte";

  onMount(() => {
    startRouter();
    session.load().catch((exc) => toast(errorText(exc), "error"));
  });

  $effect(() => {
    if (!session.loaded) return;
    const target = landing(session.me, router.current);
    if (target) navigate(target, { replace: true });
  });

  // the inbox's count in the tab's title, its favicon and the installed app's badge — what the menu's
  // inbox shows; a chat's new replies have their own badge in the menu
  $effect(() => showUnread(t("app.title"), session.me ? live.inbox : 0));

  $effect(() => {
    if (session.me) startLive();
    else {
      stopLive();
      projects.reset();
      overview.reset();
      feed.reset();
      newCardForm.reset();
    }
  });

  // every screen shows projects by name, kept current while signed in
  $effect(() => {
    if (!session.me) return;
    projects.ensure().catch(() => undefined);
    return projects.follow();
  });
  // the cabinet's models the same way — the card's chips and the projects' defaults name them
  $effect(() => {
    if (!session.me) return;
    models.ensure().catch(() => undefined);
    return models.follow();
  });
</script>

{#if !session.loaded}
  <main class="main bare"><p class="empty">{t("app.loading")}</p></main>
{:else if !session.me}
  <main class="main bare"><Login /></main>
{:else}
  <div class="app nav" class:folded={sideMenu.collapsed}>
    <Nav />
    <main class="main" class:wide={router.current.name === "board" || router.current.name === "dashboard"} class:dashboard-shell={router.current.name === "dashboard"} class:chat-shell={router.current.name === "agent" && router.current.params.name === "boss" && session.can("agents.chat")}>
      {#if !live.online}
        <p class="offline">{t("app.offline")}</p>
      {:else if live.link === "reconnecting"}
        <p class="offline" role="status">{t("app.reconnecting")}</p>
      {/if}
      {#if staleness.count > 0}
        <p class="offline" role="status">
          {staleness.oldest ? t("app.stale_at", { time: when(new Date(staleness.oldest).toISOString()) }) : t("app.stale")}
          <button type="button" class="btn small ghost" onclick={retryStale}>{t("app.retry")}</button>
        </p>
      {/if}
      {#if session.me.must_setup && router.current.name !== "setup"}
        <p class="banner">{t("onboarding.passkey")} <a href="/setup">{t("setup.title")}</a></p>
      {/if}
      {#if router.current.name === "setup"}
        <Setup />
      {:else if router.current.name === "dashboard"}
        <Dashboard />
      {:else if router.current.name === "inbox"}
        <Inbox />
      {:else if router.current.name === "board"}
        <Board />
      {:else if router.current.name === "card"}
        {#key router.current.params.id}<Card id={router.current.params.id} />{/key}
      {:else if router.current.name === "decision"}
        <Decision id={router.current.params.id} proposal={router.current.params.proposal} />
      {:else if router.current.name === "agents"}
        <Agents />
      {:else if router.current.name === "agent"}
        {#key router.current.params.name}<Agent name={router.current.params.name} />{/key}
      {:else if router.current.name === "feed"}
        <Feed />
      {:else if router.current.name === "settings"}
        <Settings />
      {:else if router.current.name === "users"}
        <SettingsUsers />
      {:else if router.current.name === "projects"}
        <SettingsProjects />
      {:else if router.current.name === "models"}
        <SettingsModels />
      {:else if router.current.name === "hindsight"}
        <SettingsHindsight />
      {:else if router.current.name === "servers"}
        <Servers />
      {:else if router.current.name === "monitor-settings"}
        <MonitorSettings />
      {:else if router.current.name === "server"}
        {#key router.current.params.alias}<Server alias={router.current.params.alias} />{/key}
      {:else if router.current.name === "incident"}
        {#key router.current.params.id}<Incident id={router.current.params.id} />{/key}
      {:else if router.current.name === "incidents"}
        <Incidents />
      {:else}
        <div class="topbar"><h1>{t("app.notfound")}</h1></div>
      {/if}
    </main>
  </div>
  <StepUp />
  <!-- the new card's form is the app's — the menu and the board's header open it -->
  {#if newCardForm.open}
    <NewCard project={newCardForm.project} cards={newCardForm.cards} onclose={() => newCardForm.close()} />
  {/if}
{/if}
<Toast />
