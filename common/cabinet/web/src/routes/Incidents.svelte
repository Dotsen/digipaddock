<script lang="ts">
  import { onMount, untrack } from "svelte";
  import Freshness from "../components/Freshness.svelte";
  import IncidentBulkBar from "../components/IncidentBulkBar.svelte";
  import IncidentRows from "../components/IncidentRows.svelte";
  import MonitorNav from "../components/MonitorNav.svelte";
  import PlatformAlerts from "../components/PlatformAlerts.svelte";
  import { apiGet } from "../lib/api";
  import { errorText, t } from "../lib/i18n.svelte";
  import { onLiveEvent, watchAll } from "../lib/live.svelte";
  import type { IncidentQuery } from "../lib/monitoring";
  import type { ServerRow } from "../lib/types";
  import { navigate, router } from "../lib/router.svelte";
  import { touchesServers } from "../lib/servers";
  import { synced } from "../lib/synced.svelte";
  import { toast } from "../lib/toast.svelte";
  let result = $state<IncidentQuery | null>(null);
  let status = $state(router.current.query.get("status") ?? "active");
  let server = $state(router.current.query.get("server") ?? "");
  let level = $state(router.current.query.get("level") ?? "");
  let query = $state(router.current.query.get("q") ?? "");
  let now = $state(Date.now());
  let processing = $state(router.current.query.get("processing") ?? "");
  let assignee = $state(router.current.query.get("assignee") ?? "");
  let since = $state(router.current.query.get("since") ?? "");
  let until = $state(router.current.query.get("until") ?? "");
  let group = $state(router.current.query.get("group") ?? ""); // the incidents of one tick and check
  let selected = $state<string[]>([]);
  let loadingMore = $state(false);
  let servers = $state<ServerRow[]>([]);
  let debounce: ReturnType<typeof setTimeout> | undefined;
  let requestQuery = "";
  let generation = 0;
  const serverData = synced(async () => {
    servers = await apiGet<ServerRow[]>("/api/servers");
  });
  const data = synced(async () => {
    const current = ++generation;
    const filter = requestQuery;
    const answer = await apiGet<IncidentQuery>(`/api/incidents/query?${filter}`);
    if (current === generation && filter === requestQuery) result = answer;
  });
  $effect(() => {
    const q = router.current.query;
    clearTimeout(debounce);
    status = q.get("status") ?? "active"; server = q.get("server") ?? ""; level = q.get("level") ?? "";
    query = q.get("q") ?? ""; processing = q.get("processing") ?? ""; assignee = q.get("assignee") ?? "";
    since = q.get("since") ?? ""; until = q.get("until") ?? ""; group = q.get("group") ?? "";
    selected = [];
    requestQuery = q.toString();
    generation += 1;
    result = null;
    loadingMore = false;
    untrack(() => void data.run());
  });
  function apply(replace = false) {
    clearTimeout(debounce);
    const params = new URLSearchParams();
    if (status !== "active") params.set("status", status);
    if (server.trim()) params.set("server", server.trim());
    if (level) params.set("level", level);
    if (query.trim()) params.set("q", query.trim());
    if (processing) params.set("processing", processing);
    if (assignee.trim()) params.set("assignee", assignee.trim());
    if (since) params.set("since", since);
    if (until) params.set("until", until);
    if (group) params.set("group", group);
    navigate(`/incidents${params.size ? `?${params}` : ""}`, {replace});
  }
  function search() {
    clearTimeout(debounce);
    debounce = setTimeout(() => apply(true), 300);
  }
  async function more() {
    if (!result?.next_cursor) return;
    loadingMore = true;
    const current = generation;
    const params = new URLSearchParams(requestQuery);
    params.set("cursor", result.next_cursor);
    try {
      const answer = await apiGet<IncidentQuery>(`/api/incidents/query?${params}`);
      if (current === generation && result) result = {...answer, items:[...result.items, ...answer.items]};
    } catch (error) { toast(errorText(error), "error"); }
    finally { loadingMore = false; }
  }
  onMount(() => {
    void serverData.run();
    const release = watchAll();
    const stop = onLiveEvent(e => { if (touchesServers(e) || e.kind.startsWith("card.")) void data.run(); });
    const tick = setInterval(() => { now = Date.now(); void data.run(); }, 60_000);
    return () => { clearTimeout(debounce); clearInterval(tick); stop(); release(); data.dispose(); serverData.dispose(); };
  });
</script>

<div class="monitor-page">
  <div class="topbar"><h1>{t("nav.incidents")}</h1><Freshness {data} /></div>
  <MonitorNav active="incidents" />
  <PlatformAlerts {now} />
  <form class="monitor-filters" onsubmit={e => {e.preventDefault(); apply(true);}}>
    <label class="field"><span>{t("mon.filter_status")}</span>
    <select aria-label={t("mon.filter_status")} bind:value={status} onchange={() => apply()}>
      <option value="active">{t("mon.active")}</option><option value="resolved">{t("mon.closed")}</option><option value="all">{t("mon.all_incidents")}</option>
      <option value="acknowledged">{t("mon.acknowledged")}</option>
    </select></label>
    <label class="field"><span>{t("mon.filter_level")}</span>
    <select aria-label={t("mon.filter_level")} bind:value={level} onchange={() => apply()}>
      <option value="">{t("mon.all_levels")}</option><option value="crit">{t("servers.level.crit")}</option><option value="warn">{t("servers.level.warn")}</option>
    </select></label>
    <label class="field"><span>{t("incident.server")}</span>
      <select aria-label={t("incident.server")} bind:value={server} onchange={() => apply()}>
        <option value="">{t("mon.all_servers")}</option>
        {#if server && !servers.some(s => s.alias === server)}<option value={server}>{server}</option>{/if}
        {#each servers as s (s.alias)}<option value={s.alias}>{s.alias}{!s.enabled ? ` · ${t("servers.state.off")}` : ""}</option>{/each}
      </select>
    </label>
    <label class="field monitor-search"><span>{t("mon.search_incidents")}</span>
      <input type="search" aria-label={t("mon.search_incidents")} placeholder={t("mon.search_incidents")} bind:value={query} oninput={search} />
    </label>
    <details class="monitor-details"><summary>{t("mon.more_filters")}</summary>
      <div class="monitor-filter-extra">
      <label class="field"><span>{t("mon.processing")}</span>
      <select aria-label={t("mon.processing")} bind:value={processing} onchange={() => apply()}>
        <option value="">{t("mon.any_processing")}</option><option value="unassigned">{t("mon.unassigned")}</option>
        <option value="assigned">{t("mon.has_work")}</option><option value="waiting">{t("mon.waiting_decision")}</option><option value="recheck">{t("mon.waiting_recheck")}</option>
      </select></label>
      <label class="field"><span>{t("mon.assignee")}</span><input type="text" aria-label={t("mon.assignee")} placeholder="@sysadmin" bind:value={assignee} oninput={search} /></label>
      <label class="field"><span>{t("mon.since_date")}</span><input class="input" type="date" bind:value={since} onchange={() => apply()} /></label>
      <label class="field"><span>{t("mon.until_date")}</span><input class="input" type="date" bind:value={until} onchange={() => apply()} /></label>
      </div>
    </details>
  </form>
  {#if serverData.error}<p class="error-text" role="alert">{errorText(serverData.error)}</p>{/if}
  {#if result}
    <p class="monitor-note" role="status">{t("mon.queue_count", {n:result.total})} · {t("servers.level.crit")}: {result.counts.crit ?? 0} · {t("servers.level.warn")}: {result.counts.warn ?? 0}</p>
    {#if group}<p class="monitor-note">{t("mon.group.members")} · <a href="/incidents">{t("mon.all_incidents")}</a></p>{/if}
    <IncidentRows items={result.items} showServer {now} selectable bind:selected />
    {#if result.next_cursor}<button class="btn" disabled={loadingMore} onclick={more}>{t("mon.more")}</button>{/if}
  {:else}<p class="empty">{data.error ? errorText(data.error) : t("app.loading")}</p>{/if}
  {#if result}
    <IncidentBulkBar items={result.items.filter((i) => selected.includes(i.id))} ondone={(keep) => ((selected = keep), void data.run())} />
  {/if}
</div>
