<script lang="ts">
  import { getLocale, t } from "../lib/i18n.svelte";
  import { groupHref, groupRows } from "../lib/incidentGroups";
  import { checkTitle, levelText, notificationText, resolutionText } from "../lib/monitoring";
  import { incidentHref } from "../lib/servers";
  import { session } from "../lib/session.svelte";
  import { age } from "../lib/time";
  import type { Incident } from "../lib/types";
  // the incidents one tick opened for one check are one row that unfolds into its members; with
  // `selectable` (and the right to acknowledge) each incident and each whole group can be picked for a bulk action
  let {
    items, showServer = false, now = Date.now(), selectable = false, selected = $bindable([]),
  }: { items: Incident[]; showServer?: boolean; now?: number; selectable?: boolean; selected?: string[] } = $props();
  const firstSeen = (iso: string) => new Date(iso).toLocaleString(getLocale() === "ru" ? "ru-RU" : "en-GB", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  });
  const rows = $derived(groupRows(items));
  const picking = $derived(selectable && session.can("incidents.ack"));
  const toggle = (id: string) => (selected = selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id]);
  // an incident the owner signed into the baseline waits for the monitor's tick — nothing left to pick
  const pickable = (members: Incident[]) => members.filter((m) => !m.accepted);
  const allOf = (members: Incident[]) => pickable(members).every((m) => selected.includes(m.id));
  function toggleGroup(members: Incident[]) {
    const ids = pickable(members).map((m) => m.id);
    selected = allOf(members) ? selected.filter((x) => !ids.includes(x)) : [...new Set([...selected, ...ids])];
  }
  // the server stands beside the number as a chip — the same check on two servers must not read as one
  // event twice; a group names the servers of its loaded members and counts the rest
  const memberServers = (members: Incident[]) => [...new Set(members.map((m) => m.server))];
</script>

{#snippet card(i: Incident)}
  <div class="incident-li" class:picking>
    {#if picking && !i.accepted}
      <label class="incident-pick"><input type="checkbox" checked={selected.includes(i.id)} onchange={() => toggle(i.id)}
        aria-label={t("mon.bulk.pick", { id: i.id })} /></label>
    {:else if picking}<span class="incident-pick"></span>
    {/if}
    <a class="monitor-row incident-card" class:critical={i.level === "crit"} class:closed={i.status === "resolved"} href={incidentHref(i.id)} aria-labelledby="incident-number-{i.n} incident-title-{i.n}">
    <div class="incident-card-top">
      <span class="incident-card-id"><b id="incident-number-{i.n}">{i.id}</b>
        {#if showServer}<span class="chip text incident-server">{i.server}</span>{/if}</span>
      <span class="incident-card-seen">{t("mon.first_seen")}: <time datetime={i.opened_at}>{firstSeen(i.opened_at)}</time></span>
    </div>
    <div class="monitor-row-head">
      <span class="chip monitor-badge" class:active={i.status !== "resolved"} class:good={i.status === "resolved"}>{i.status === "resolved" ? t("mon.closed") : t("mon.active")}</span>
      <span class="chip monitor-badge {i.level}">{levelText(i.level)}</span>
      {#if i.accepted}<span class="chip monitor-badge good">{t(i.accepted.kind === "revoke" ? "mon.revoked_waiting" : "mon.accepted_waiting")}</span>{/if}
    </div>
    <h3 class="incident-card-title" id="incident-title-{i.n}">{checkTitle(i.check, i.subject)}</h3>
    <p>{i.investigation?.report?.summary || i.current?.value || i.value || i.text || "—"}</p>
    <div class="monitor-meta">
      {#if i.investigation}<span>{t(`mon.analysis.${i.investigation.state}`)}</span>{/if}
      <span>{t("mon.since", { age: age(i.opened_at, now) })}</span>
      {#if i.status === "resolved"}<span>{resolutionText(i)}</span>
      {:else}
        {#if i.assignee}<span>{i.assignee}</span>{/if}
        {#if i.delivery}<span>{t(`mon.delivery.${i.delivery.state}` as import("../lib/i18n.svelte").Key)}</span>{/if}
        {#if notificationText(i, now)}<span>{notificationText(i, now)}</span>{/if}
        {#if i.freshness && i.freshness !== "fresh"}<span>{t("mon.data_attention")}</span>{/if}
      {/if}
    </div>
    <span class="btn incident-card-open">{t("mon.open_incident")}</span>
    </a>
  </div>
{/snippet}

<ul class="monitor-rows">
  {#each rows as row (row.kind === "one" ? row.incident.n : row.id)}
    <li>
      {#if row.kind === "one"}{@render card(row.incident)}
      {:else}
        <details class="monitor-row incident-group" class:critical={row.level === "crit"}>
          <summary>
            <div class="incident-card-top">
              <span class="incident-card-id"><b>{row.members[0].id} · {t("mon.group.count", { n: row.size })}</b>
                {#if showServer}
                  {#each memberServers(row.members) as s (s)}<span class="chip text incident-server">{s}</span>{/each}
                  {#if row.servers > memberServers(row.members).length}<span class="chip">+{row.servers - memberServers(row.members).length}</span>{/if}
                {/if}</span>
              <span class="incident-card-seen">{t("mon.first_seen")}: <time datetime={row.opened_at}>{firstSeen(row.opened_at)}</time></span>
            </div>
            <div class="monitor-row-head"><span class="chip monitor-badge {row.level}">{levelText(row.level)}</span>
              {#if row.subjects > 1}<span class="chip">{t("mon.group.objects", { n: row.subjects })}</span>{/if}</div>
            <h3 class="incident-card-title">{t("mon.group.title", { title: checkTitle(row.check, row.subjects > 1 ? "" : row.subject), servers: row.servers })}</h3>
            <div class="monitor-meta"><span>{t("mon.since", { age: age(row.opened_at, now) })}</span></div>
          </summary>
          <div class="incident-group-tools">
            {#if picking && pickable(row.members).length}
              <label class="incident-group-pick"><input type="checkbox" checked={allOf(row.members)} onchange={() => toggleGroup(row.members)} />
                {t("mon.group.select")}</label>
            {/if}
            {#if row.members.length < row.size}
              <a href={groupHref(row.id)}>{t("mon.group.all", { n: row.size })}</a>
            {/if}
          </div>
          <ul class="monitor-rows" aria-label={t("mon.group.members")}>
            {#each row.members as m (m.n)}<li>{@render card(m)}</li>{/each}
          </ul>
        </details>
      {/if}
    </li>
  {:else}<li class="monitor-empty">{t("mon.no_incidents")}</li>{/each}
</ul>
