<script lang="ts">
  // The servers the monitor checks: the worst first, each with its worst check, its open incidents and
  // when it last answered. Live events of incidents and "Check now" refresh it; the monitor's ticks send none,
  // so it also reads itself once a minute. Below them, the dev.check contours for a reader who may see them.
  import { onMount } from "svelte";
  import Contours from "../components/Contours.svelte";
  import Freshness from "../components/Freshness.svelte";
  import MonitorNav from "../components/MonitorNav.svelte";
  import { apiGet } from "../lib/api";
  import { coalesce } from "../lib/coalesce";
  import { errorText, t } from "../lib/i18n.svelte";
  import { onLiveEvent, watchAll } from "../lib/live.svelte";
  import { checkTitle } from "../lib/monitoring";
  import { serverHref, shortValue, sortServers, touchesServers } from "../lib/servers";
  import { synced } from "../lib/synced.svelte";
  import { age, when } from "../lib/time";
  import type { ContourRow, ServerRow } from "../lib/types";

  let rows = $state<ServerRow[] | null>(null);
  let contours = $state<ContourRow[]>([]);
  let search = $state("");
  let filter = $state("all");
  const visible = $derived(rows?.filter(r => `${r.alias} ${r.host}`.toLowerCase().includes(search.toLowerCase()))
    .filter(r => filter === "all" || (filter === "problems" && (r.worst !== "ok" || !r.reachable || r.incidents > 0 || r.freshness !== "fresh" || r.coverage_freshness !== "fresh"))
      || (filter === "stale" && r.freshness !== "fresh") || (filter === "off" && !r.enabled)) ?? []);
  const data = synced(async () => {
    const [servers, found] = await Promise.all([apiGet<ServerRow[]>("/api/servers"),
      apiGet<ContourRow[]>("/api/contours").catch(() => [])]);  // not allowed or not there: the servers alone
    rows = sortServers(servers);
    contours = found;
  });
  const refresh = coalesce(() => void data.run());

  onMount(() => {
    void data.run();
    const release = watchAll();
    const stop = onLiveEvent((event) => touchesServers(event) && refresh());
    const tick = setInterval(() => void data.run(), 60_000);
    return () => {
      clearInterval(tick);
      refresh.cancel();
      stop();
      release();
      data.dispose();
    };
  });

  // never a local binding named `state`: Svelte 5 would read `$state(...)` above as a store subscription to it, and
  // the page would fail as it is created (/servers did not open)
  function levelOf(row: ServerRow): { key: string; cls: string } {
    if (!row.enabled) return { key: t("servers.state.off"), cls: "off" };
    if (row.freshness && row.freshness !== "fresh") return { key: t("mon.stale_server"), cls: "off" };
    if (row.coverage_freshness && row.coverage_freshness !== "fresh" && row.worst === "ok") return { key: t("mon.data_incomplete"), cls:"off" };
    if (!row.reachable) return { key: t("servers.state.down"), cls: "crit" };
    if (!row.worst) return { key: t("servers.state.none"), cls: "off" };
    return { key: t(`servers.level.${row.worst}`), cls: row.worst };
  }
</script>

<div class="monitor-page">
<div class="topbar">
  <h1>{t("nav.servers")}</h1>
  <Freshness {data} />
  <span class="spacer"></span>
  <a class="btn small ghost" href="/servers/settings">{t("servers.settings")}</a>
</div>
<MonitorNav active="servers" />
<div class="monitor-filters">
  <input type="search" aria-label={t("mon.search_servers")} placeholder={t("mon.search_servers")} bind:value={search} />
  <select aria-label={t("mon.filter_servers")} bind:value={filter}>
    <option value="all">{t("mon.all_servers")}</option><option value="problems">{t("mon.attention")}</option>
    <option value="stale">{t("mon.stale_server")}</option><option value="off">{t("servers.state.off")}</option>
  </select>
</div>

{#if rows === null}
  {#if data.error}<p class="empty" role="alert">{errorText(data.error)}</p>{:else}<p class="empty">{t("app.loading")}</p>{/if}
{:else if rows.length === 0}
  <p class="empty">{t("servers.empty")}</p>
{:else}
  <div class="list">
    {#each visible as row (row.alias)}
      {@const s = levelOf(row)}
      <a class="item" class:dim={!row.enabled} href={serverHref(row.alias)}>
        <span class="chip text level {s.cls}">{s.key}</span>
        <span class="body">
          <b>{row.alias}</b>
          <span class="muted line">
            {t("servers.checks_count", row.user_checks ?? row.checks)}
            {#if row.last_ok} · <span title={when(row.last_ok)}>{t("servers.last_ok", { age: age(row.last_ok) })}</span>{/if}
          </span>
          {#if row.reason}<span class="line">{checkTitle(row.reason.check, row.reason.subject)}: {shortValue(row.reason.value) || row.reason.text || t("mon.data_attention")}</span>{/if}
        </span>
        {#if row.incidents}<span class="chip" class:crit={row.worst === "crit"}>{t("servers.incidents", { n: row.incidents })}</span>{/if}
      </a>
    {/each}
  </div>
{/if}
<Contours rows={contours} />
</div>

<style>
  .list { display: flex; flex-direction: column; gap: var(--sp-8); }
  .item {
    display: grid; grid-template-columns: minmax(0, 128px) minmax(0, 1fr) auto; gap: var(--sp-10); align-items: center; padding: var(--sp-12);
    background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); color: var(--text);
    text-decoration: none;
  }
  .item:hover { border-color: var(--accent); }
  .item.dim { opacity: .55; }
  .body { display: grid; gap: var(--sp-2); min-width: 0; }
  .line { font-size: var(--fs-sm); overflow-wrap: anywhere; }
  .level { justify-content: center; text-align: center; min-width: 0; }
  .level.crit { color: var(--crit); background: var(--crit-bg); }
  .level.warn { color: var(--warn); background: var(--warn-bg); }
  .level.ok { color: var(--good); background: var(--good-bg); }
  .level.off { color: var(--muted); }
  .chip.crit { color: var(--crit); background: var(--crit-bg); }
  @media (max-width: 599px) {
    .item { grid-template-columns: minmax(0, 1fr); align-items: start; }
    .level, .item > .chip { justify-self: start; }
  }
</style>
