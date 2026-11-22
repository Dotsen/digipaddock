<script lang="ts">
  import { onMount } from "svelte";
  import BackButton from "../components/BackButton.svelte";
  import Freshness from "../components/Freshness.svelte";
  import IncidentBulkBar from "../components/IncidentBulkBar.svelte";
  import IncidentRows from "../components/IncidentRows.svelte";
  import LinkedCards from "../components/LinkedCards.svelte";
  import MonitorNav from "../components/MonitorNav.svelte";
  import SignDialog from "../components/SignDialog.svelte";
  import TrustList from "../components/TrustList.svelte";
  import { apiGet, apiPost, ApiError } from "../lib/api";
  import { errorText, t, type Key } from "../lib/i18n.svelte";
  import { onLiveEvent, watchAll } from "../lib/live.svelte";
  import { activeCards, activeIncidents, checkTitle, isDiagnostic, levelText, needsAttention, thresholdLine } from "../lib/monitoring";
  import { checking, shortValue, touchesServers } from "../lib/servers";
  import { session } from "../lib/session.svelte";
  import { navigate, router } from "../lib/router.svelte";
  import { synced } from "../lib/synced.svelte";
  import { age, when } from "../lib/time";
  import { toast } from "../lib/toast.svelte";
  import { propose, removeAddress, type TrustRequest } from "../lib/trust";
  import type { CheckRequest, DecisionItem, ServerPage } from "../lib/types";

  let { alias }: { alias: string } = $props();
  let page = $state<ServerPage | null>(null);
  let missing = $state(false);
  let busy = $state(false);
  let allChecks = $state(false);
  let allIncidents = $state(false);
  let trustTab = $state("trusted");
  let trustSearch = $state("");
  let trustShown = $state(50);
  let historyShown = $state(100);
  let adding = $state(false);
  let cidr = $state("");
  let label = $state("");
  let signing = $state<DecisionItem | null>(null);
  let baselinePreview = $state(false);
  let now = $state(Date.now());
  const lists = ["trusted", "identities", "ports", "wgpeers"] as const;
  const data = synced(async () => {
    try { page = await apiGet<ServerPage>(`/api/servers/${encodeURIComponent(alias)}`); missing = false; }
    catch (error) { if (error instanceof ApiError && error.status === 404) missing = true; else throw error; }
  });
  onMount(() => {
    void data.run();
    const release = watchAll();
    const stop = onLiveEvent(e => { if (touchesServers(e) || e.kind.startsWith("card.")) void data.run(); });
    const tick = setInterval(() => { now = Date.now(); void data.run(); }, 60_000);
    return () => { clearInterval(tick); stop(); release(); data.dispose(); };
  });
  const canAct = $derived(session.can("incidents.ack"));
  let selected = $state<string[]>([]); // the incidents picked for a bulk action
  const canTrust = $derived(session.can("decide.monitor_trust"));
  const checks = $derived([...(page?.checks ?? []), ...(page?.missing_checks ?? []).map(check => ({server:alias, check, subject:"", at:"", level:"ok" as const, value:"", text:t("mon.never_measured"), freshness:"unknown"}))]);
  const useful = $derived(checks.filter(c => !isDiagnostic(c.check)));
  const problems = $derived(checks.filter(needsAttention).sort((a, b) =>
    (a.level === "crit" ? 0 : a.freshness === "error" ? 1 : a.level === "warn" ? 2 : 3) -
    (b.level === "crit" ? 0 : b.freshness === "error" ? 1 : b.level === "warn" ? 2 : 3)));
  const visibleChecks = $derived(allChecks ? useful : problems);
  const incidents = $derived(page ? allIncidents ? page.incidents : activeIncidents(page.incidents) : []);
  const entries = $derived(page && lists.includes(trustTab as typeof lists[number])
    ? page.trust[trustTab as typeof lists[number]].filter(r => `${r.item} ${r.label}`.toLowerCase().includes(trustSearch.toLowerCase())) : []);
  $effect(() => {
    const q = router.current.query;
    allChecks = q.get("checks") === "all";
    allIncidents = q.get("incidents") === "all";
    const requested = q.get("trust") ?? "trusted";
    trustTab = [...lists, "history"].includes(requested) ? requested : "trusted";
  });
  function filterUrl(key: string, value: string) {
    const q = new URLSearchParams(router.current.query);
    if (value) q.set(key, value); else q.delete(key);
    navigate(`/servers/${encodeURIComponent(alias)}${q.size ? `?${q}` : ""}`);
  }

  async function checkNow() {
    busy = true;
    try {
      const answer = await apiPost<{ request: CheckRequest }>(`/api/servers/${encodeURIComponent(alias)}/check`, {});
      if (page) page = { ...page, check_now: answer.request };
    } catch (error) { toast(errorText(error), "error"); } finally { busy = false; }
  }
  async function trust(request: TrustRequest) {
    busy = true;
    try { signing = await propose(request); adding = false; baselinePreview = false; }
    catch (error) { toast(errorText(error), "error"); } finally { busy = false; }
  }
  async function remove(item: string) {
    if (!confirm(t("trust.remove.confirm", { cidr: item }))) return;
    busy = true;
    try { await removeAddress(item); await data.run(); }
    catch (error) { toast(errorText(error), "error"); } finally { busy = false; }
  }
  function signed() { signing = null; cidr = label = ""; void data.run(); }
  function selectTrust(name: string) { filterUrl("trust", name === "trusted" ? "" : name); trustSearch = ""; trustShown = 50; }
  function tabKey(event: KeyboardEvent, name: string) {
    const names: string[] = [...lists, "history"];
    const index = names.indexOf(name);
    const next = event.key === "ArrowRight" ? names[(index + 1) % names.length] : event.key === "ArrowLeft" ? names[(index + names.length - 1) % names.length]
      : event.key === "Home" ? names[0] : event.key === "End" ? names[names.length - 1] : "";
    if (next) { event.preventDefault(); selectTrust(next); document.getElementById(`trust-tab-${next}`)?.focus(); }
  }
</script>

<div class="monitor-page">
  <div class="topbar">
    <BackButton /><h1 class="monitor-title">{alias}</h1><span class="spacer"></span>
    {#if page?.server.enabled && canAct}<button class="btn primary" disabled={busy || checking(page.check_now)} onclick={checkNow}>{t("server.check_now")}</button>{/if}
  </div>
  <MonitorNav active="servers" />
  {#if missing}<p class="empty">{t("server.notfound")}</p>
  {:else if !page}<p class="empty">{data.error ? errorText(data.error) : t("app.loading")}</p>
  {:else}
    <section class="monitor-summary" aria-label={t("mon.summary")}>
      <div class="monitor-row-head">
        <span class="chip monitor-badge" class:good={page.server.freshness === "fresh" && !!page.server.reachable}>
          {!page.server.enabled ? t("servers.state.off") : page.server.freshness !== "fresh" ? t("mon.stale_server") : page.server.reachable ? t("mon.reachable") : t("servers.state.down")}
        </span>
        {#if problems.length}<span class="chip monitor-badge {page.server.worst}">{t("mon.problem_count", { n: problems.length })}</span>{/if}
      </div>
      {#if problems.length}<p><b>{checkTitle(problems[0].check, problems[0].subject)}</b> · {shortValue(problems[0].value) || problems[0].text}</p>
      {:else}<p>{page.server.freshness === "fresh" ? t("mon.no_problems") : t("mon.data_attention")}</p>{/if}
      <div class="monitor-meta">
        <span>{t("server.host")}: {page.server.host || "—"}</span>
        {#if page.server.last_ok}<span title={when(page.server.last_ok)}>{t("mon.last_response", { age: age(page.server.last_ok) })}</span>{/if}
        <a href="/incidents?level=&server={encodeURIComponent(alias)}">{t("mon.active_count", { n: activeIncidents(page.incidents).length })}</a>
        <span>{t("mon.card_count", { n: activeCards(page.cards).length })}</span>
      </div>
      <p class="monitor-note">{t("mon.observed", { at: page.server.observed_at ? when(page.server.observed_at) : t("mon.unknown") })}</p>
      {#if page.server.monitor_freshness !== "fresh"}<p class="monitor-warning">{t("mon.monitor_stale")}</p>{/if}
      {#if page.server.baseline_state === "unavailable"}<p class="monitor-warning">{t("mon.baseline_unavailable")}</p>{/if}
      <a href="/servers/settings?server={encodeURIComponent(alias)}">{t("servers.settings")}</a>
    </section>
    {#if page.check_now}
      <p role="status">{t("server.check.status", { state: t(`server.check.state.${page.check_now.state}` as Key), age: age(page.check_now.finished || page.check_now.started || page.check_now.created) })}
        {#if page.check_now.state === "failed"} · {page.check_now.detail === "timeout" ? t("server.check.detail.timeout") : page.check_now.detail}{/if}
      </p>
    {/if}
    <section class="monitor-section">
      <h2>{t("server.checks")}</h2>
      <div class="monitor-switch" role="group" aria-label={t("server.checks")}>
        <button class="btn" class:chosen={!allChecks} aria-pressed={!allChecks} onclick={() => filterUrl("checks", "")}>{t("mon.attention")} ({problems.length})</button>
        <button class="btn" class:chosen={allChecks} aria-pressed={allChecks} onclick={() => filterUrl("checks", "all")}>{t("mon.all_checks")} ({useful.length})</button>
      </div>
      <ul class="monitor-rows">
        {#each visibleChecks as c (c.check + "|" + c.subject)}
          <li class="monitor-row">
            <div class="monitor-row-head"><span class="chip monitor-badge {c.level}">{levelText(c.level)}</span><b>{checkTitle(c.check, c.subject)}</b></div>
            <p>{shortValue(c.value) || c.text || t("mon.no_changes")}</p>
            {#if thresholdLine(c)}<p class="monitor-note">{thresholdLine(c)}</p>{/if}
            {#if c.text && c.text !== c.value}<p class="monitor-note">{c.text}</p>{/if}
            <div class="monitor-meta"><span title={when(c.at)}>{c.at ? t("mon.measured", { age: age(c.at) }) : t("mon.unknown")}</span>
              {#if c.freshness !== "fresh"}<span>{t(`mon.freshness.${c.freshness || "unknown"}` as Key)}</span>{/if}
              {#each page.incidents.filter(i => i.status !== "resolved" && i.check === c.check && (i.subject === c.subject || (!c.subject && i.subject === "-"))) as i (i.n)}<a href="/incidents/{i.id}">{i.id} · {t("mon.open_incident")}</a>{/each}
            </div>
          </li>
        {:else}<li class="monitor-empty">{t("mon.no_problems")}</li>{/each}
      </ul>
      <details class="monitor-details"><summary>{t("mon.collection_diagnostics")}</summary>
        {#each page.checks.filter(c => isDiagnostic(c.check)) as c (c.check + c.subject)}<p>{checkTitle(c.check, c.subject)} · {levelText(c.level)} · {when(c.at)} {c.text}</p>{/each}
        <p>{t("mon.full_cycle", { at: page.monitor_full_at ? when(page.monitor_full_at) : t("mon.unknown") })}</p>
        {#if page.diagnostics?.queued_alerts !== undefined}<p>{t("mon.delivery_backlog", {n:page.diagnostics.queued_alerts, changes:page.diagnostics.queued_changes ?? 0})}</p>{/if}
        <Freshness {data} />
      </details>
    </section>
    <section class="monitor-section"><h2>{t("server.incidents")}</h2>
      <div class="monitor-switch" role="group" aria-label={t("server.incidents")}>
        <button class="btn" class:chosen={!allIncidents} aria-pressed={!allIncidents} onclick={() => filterUrl("incidents", "")}>{t("mon.active")} ({activeIncidents(page.incidents).length})</button>
        <button class="btn" class:chosen={allIncidents} aria-pressed={allIncidents} onclick={() => filterUrl("incidents", "all")}>{t("mon.all_incidents")}</button>
        <a class="btn ghost" href="/incidents?level=&server={encodeURIComponent(alias)}">{t("mon.full_queue")}</a>
      </div><IncidentRows items={incidents} {now} selectable bind:selected />
      <IncidentBulkBar items={incidents.filter((i) => selected.includes(i.id))} ondone={(keep) => ((selected = keep), void data.run())} />
    </section>
    <section class="monitor-section"><h2>{t("server.cards")}</h2><LinkedCards cards={page.cards} /></section>
    <section class="monitor-section"><h2>{t("mon.baseline")}</h2>
      <p class="monitor-note">{t("mon.baseline_help")}</p>
      <p class="monitor-note">{page.trust.taken_at ? t("server.trust.taken_at", { when: when(page.trust.taken_at) }) : t("server.trust.no_baseline")}</p>
      <div class="monitor-tabs" role="tablist" aria-label={t("mon.baseline")}>
        {#each [...lists, "history"] as name}
          <button class="btn" role="tab" id="trust-tab-{name}" aria-controls="trust-panel" aria-selected={trustTab === name} tabindex={trustTab === name ? 0 : -1} onkeydown={e => tabKey(e, name)} onclick={() => selectTrust(name)}>{name === "history" ? t("mon.trust_history") : t(`server.trust.${name}` as Key)} {#if name !== "history"}({page.trust[name as typeof lists[number]].length}){/if}</button>
        {/each}
      </div>
      <div role="tabpanel" id="trust-panel" aria-labelledby="trust-tab-{trustTab}" tabindex="0">
        {#if trustTab === "history"}
          <TrustList decisions={page.proposals?.decisions ?? []} removals={page.proposals?.removals ?? []} />
          {#if canTrust && page.server.enabled}
            <button class="btn" onclick={() => baselinePreview = !baselinePreview}>{t("trust.button.baseline")}</button>
            {#if baselinePreview}<p class="monitor-note">{t("mon.baseline_warning", { server: alias })}</p><button class="btn" disabled={busy} onclick={() => trust({kind: "baseline", server: alias})}>{t("mon.review_sign")}</button>{/if}
          {/if}
        {:else}
          {#if trustTab === "trusted"}<p class="monitor-note">{t("mon.global_addresses")}</p>{/if}
          <div class="monitor-filters"><input type="search" aria-label={t("mon.search_objects")} placeholder={t("mon.search_objects")} bind:value={trustSearch} oninput={() => trustShown = 50} /></div>
          <ul class="monitor-rows">{#each entries.slice(0, trustShown) as r (r.item)}<li class="monitor-row"><code>{r.item}</code> {r.label}
            {#if trustTab === "trusted" && canTrust && r.label !== "stage"}<button class="btn ghost" disabled={busy} onclick={() => remove(r.item)}>{t("trust.button.remove")}</button>{/if}
          </li>{:else}<li class="monitor-empty">{t("server.trust.empty_list")}</li>{/each}</ul>
          {#if entries.length > trustShown}<button class="btn" onclick={() => trustShown += 50}>{t("mon.more")}</button>{/if}
          {#if trustTab === "trusted" && canTrust}<button class="btn" onclick={() => adding = !adding}>{t("trust.button.add")}</button>{/if}
          {#if adding && trustTab === "trusted"}<form class="monitor-form" onsubmit={e => { e.preventDefault(); void trust({kind:"trust", cidr:cidr.trim(), label:label.trim()}); }}>
            <p>{t("mon.global_addresses")}</p><input type="text" aria-label={t("trust.add.cidr")} placeholder={t("trust.add.cidr")} bind:value={cidr} required maxlength="64" />
            <input type="text" aria-label={t("trust.add.label")} placeholder={t("trust.add.label")} bind:value={label} required maxlength="200" />
            <button class="btn" disabled={busy || !cidr.trim() || !label.trim()}>{t("mon.review_sign")}</button>
          </form>{/if}
        {/if}
      </div>
    </section>
    <details class="monitor-details"><summary>{t("server.history")} ({page.history.length})</summary>
      {#each page.history.slice(0, historyShown) as h (h.id)}<p>{when(h.at)} · {checkTitle(h.check, h.subject)} · {levelText(h.prev)} → {levelText(h.level)} · {h.value}</p>{/each}
      {#if page.history.length > historyShown}<button class="btn" onclick={() => historyShown += 100}>{t("server.history.more", {n: page.history.length - historyShown})}</button>{/if}
    </details>
  {/if}
</div>
{#if signing}<SignDialog items={[signing]} onclose={() => signing = null} ondone={signed} onreread={signed} />{/if}
