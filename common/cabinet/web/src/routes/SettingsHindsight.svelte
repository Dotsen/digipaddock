<script lang="ts">
  import { onMount } from 'svelte';
  import BackButton from '../components/BackButton.svelte';
  import UsageChart from '../components/UsageChart.svelte';
  import { apiGet } from '../lib/api';
  import { errorText, t, type Key } from '../lib/i18n.svelte';
  import { releaseState, type HindsightStatus } from '../lib/hindsight';
  import { session } from '../lib/session.svelte';
  import { when } from '../lib/time';

  let data = $state<HindsightStatus | null>(null);
  let error = $state('');
  let busy = $state(false);
  let now = $state(Date.now());
  let alive = true;
  // a host without Hindsight has no snapshot to read; the page says so
  const permitted = $derived(session.can('settings') && !!session.me?.memory);
  const stale = $derived(data?.stale || !!error || !data?.collected_at ||
    !Number.isFinite(Date.parse(data.collected_at)) || now - Date.parse(data.collected_at) > 900_000);
  const value = (n: number | undefined) => n === undefined ? '—' : n.toLocaleString();
  const policy = (v: boolean | undefined) => t(v === undefined ? 'hindsight.unknown' : v ? 'hindsight.on' : 'hindsight.off');
  const unit = (state: string) => t(`hindsight.unit.${state}` as Key);
  const release = (installed?: string | null, latest?: string | null) =>
    t(`hindsight.release.${releaseState(installed, latest)}` as Key);

  async function refresh() {
    now = Date.now();
    if (busy) return;
    busy = true;
    try {
      const found = await apiGet<HindsightStatus>('/api/hindsight');
      if (alive) { data = found; error = ''; }
    } catch (exc) {
      if (alive) error = errorText(exc);
    } finally {
      if (alive) busy = false;
    }
  }

  onMount(() => {
    alive = true;
    if (permitted) void refresh();
    const timer = setInterval(() => { if (permitted && !document.hidden) void refresh(); }, 60_000);
    return () => { alive = false; clearInterval(timer); };
  });
</script>

<div class="topbar"><BackButton /><h1>Hindsight</h1>
  {#if permitted}<button class="btn" disabled={busy} onclick={refresh}>{t('hindsight.refresh')}</button>{/if}
</div>

{#if !session.can('settings')}
  <p class="empty">{t('error.forbidden')}</p>
{:else if !session.me?.memory}
  <p class="empty">{t('hindsight.not_installed')}</p>
{:else}
  <div class="stack sections">
    {#if error}<p class="banner" role="alert">{error}</p>{/if}
    {#if !data && busy}<p class="muted">{t('app.loading')}</p>{/if}
    {#if data}
      {#if stale}<p class="banner" role="status">{t('hindsight.stale')}</p>{/if}
      <section class="panel stack">
        <div class="row between"><h2>{t('hindsight.status')}</h2>
          <span class="chip" class:good={!stale && data.server?.state === 'online'}>
            {stale ? t('hindsight.unknown') : data.server?.state === 'online' ? t('hindsight.online') : t('hindsight.unavailable')}
          </span>
        </div>
        <p class="muted">{data.collected_at ? t('hindsight.collected', { at: when(data.collected_at) }) : t('hindsight.no_snapshot')}</p>
        <p class="muted small">{t('hindsight.refresh_hint')}</p>
        {#if data.server?.error}<code>{data.server.error}</code>{/if}
        <dl class="facts">
          {#each Object.entries(data.units ?? {}) as [name, state] (name)}
            <div><dt>{name}</dt><dd>{unit(state)}</dd></div>
          {/each}
        </dl>
        <p class="small muted">{t('hindsight.backup_hint')}</p>
      </section>

      <section class="panel stack">
        <h2>{t('hindsight.software')}</h2>
        <div class="row between"><span>{t('hindsight.server_version')}: <b>{data.server?.version ?? '—'}</b></span>
          <span>{release(data.server?.version, data.releases?.server)}</span></div>
        <p class="muted small">{t('hindsight.server_manual')}</p>
        <p class="muted small">{t('hindsight.latest', { server: data.releases?.server ?? '—', client: data.releases?.client ?? '—',
          at: data.releases?.checked_at ? when(data.releases.checked_at) : '—' })}</p>
        <div class="wrap"><table>
          <thead><tr><th>{t('hindsight.agent')}</th><th>{t('hindsight.version')}</th><th>{t('hindsight.release')}</th><th>{t('hindsight.autoupdate')}</th></tr></thead>
          <tbody>{#each data.clients as client (client.agent)}<tr>
            <td><a href="/agents/{encodeURIComponent(client.agent)}">@{client.agent}</a></td>
            <td>{client.version ?? '—'}</td><td>{release(client.version, data.releases?.client)}</td><td>{policy(client.policy.autoUpdate)}</td>
          </tr>{/each}</tbody>
        </table></div>
      </section>

      <section class="panel stack">
        <div class="row between"><h2>{t('hindsight.banks')}</h2><span class="chip">{data.banks.length}</span></div>
        <p class="muted">{t('hindsight.bank_model')}</p>
        {#if !data.banks_complete}<p class="banner">{t('hindsight.incomplete')} {data.banks_error ?? ''}</p>{/if}
        {#if data.banks.length === 0}<p class="muted">{t('hindsight.no_banks')}</p>{/if}
        {#each data.banks as bank (bank.id)}
          <article class="bank stack">
            <div class="row between"><h3>{bank.name || bank.id}</h3>
              {#if bank.missing}<span class="chip">{t(data.banks_complete ? 'hindsight.bank_missing' : 'hindsight.bank_unconfirmed')}</span>{/if}
            </div>
            {#if bank.name}<code>{bank.id}</code>{/if}
            <div class="row">{#each bank.agents as agent (agent)}<a href="/agents/{encodeURIComponent(agent)}">@{agent}</a>{:else}<span class="muted">{t('hindsight.no_agents')}</span>{/each}</div>
            {#if bank.projects?.length}<div class="row">{#each bank.projects as project (project)}<span class="chip">{t('hindsight.bank_project', { id: project })}</span>{/each}</div>{/if}
            <dl class="facts">
              <div><dt>{t('hindsight.documents')}</dt><dd>{value(bank.stats?.total_documents)}</dd></div>
              <div><dt>{t('hindsight.facts')}</dt><dd>{value(bank.stats?.total_nodes)}</dd></div>
              <div><dt>{t('hindsight.observations')}</dt><dd>{value(bank.stats?.total_observations)}</dd></div>
              <div><dt>{t('hindsight.pending')}</dt><dd>{value(bank.stats?.pending_operations)} / {value(bank.stats?.pending_consolidation)}</dd></div>
              <div><dt>{t('hindsight.failed')}</dt><dd class:problem={(bank.stats?.failed_operations ?? 0) + (bank.stats?.failed_consolidation ?? 0) > 0}>{value(bank.stats?.failed_operations)} / {value(bank.stats?.failed_consolidation)}</dd></div>
              <div><dt>{t('hindsight.last_write')}</dt><dd>{bank.last_write ? when(bank.last_write) : '—'}</dd></div>
            </dl>
            {#if bank.stats_error}<p class="small muted">{t('hindsight.stats_error')} <code>{bank.stats_error}</code></p>{/if}
            <h4>{t('hindsight.activity')}</h4>
            {#if bank.activity !== null}
              <div class="row">{#each Object.entries(bank.activity) as [action, count] (action)}<span class="chip">{action}: {value(count)}</span>{:else}<span class="muted">{t('hindsight.no_activity')}</span>{/each}</div>
            {:else}<p class="small muted">{t(data.server?.audit_log ? 'hindsight.activity_error' : 'hindsight.audit_off')} {bank.activity_error ?? ''}</p>{/if}
          </article>
        {/each}
      </section>

      <section class="panel stack">
        <div class="row between"><h2>{t('settings.projects')}</h2><a class="btn small" href="/settings/projects">{t('action.open')}</a></div>
        <p class="small muted">{t('hindsight.project_binding_hint')}</p>
        <div class="wrap"><table><thead><tr><th>{t('hindsight.project')}</th><th>{t('hindsight.route')}</th><th>{t('hindsight.project_bank')}</th></tr></thead>
          <tbody>{#each data.projects as project (project.id)}<tr><td>{project.name}<br /><code>{project.id}</code></td><td>{project.route}</td><td>{project.bank ?? t('hindsight.role_bank')}</td></tr>{/each}</tbody>
        </table></div>
      </section>

      <section class="panel stack">
        <h2>{t('hindsight.ingestion')}</h2>
        <p class="small muted">{t('hindsight.ingestion_hint')}</p>
        {#each data.clients as client (client.agent)}
          <details><summary>@{client.agent} · {client.enabled ? t('hindsight.on') : t('hindsight.off')}</summary>
            <dl class="facts">
              <div><dt>{t('hindsight.project_bank')}</dt><dd>{client.bank ?? '—'}</dd></div>
              <div><dt>{t('hindsight.transcripts')}</dt><dd>{policy(client.policy.retainSessions)}</dd></div>
              <div><dt>Git</dt><dd>{client.policy.gitIngest ?? '—'}</dd></div>
              <div><dt>{t('hindsight.seed')}</dt><dd>{policy(client.policy.autoSeed)}</dd></div>
              <div><dt>{t('hindsight.survey')}</dt><dd>{policy(client.policy.codebaseSurvey)}</dd></div>
              <div><dt>{t('hindsight.pages')}</dt><dd>{client.policy.pageTriggerCron ?? client.policy.pageTriggerType ?? '—'}</dd></div>
            </dl>
          </details>
        {/each}
      </section>

      <section class="panel stack">
        <h2>{t('hindsight.spend')}</h2>
        <p class="muted small">{data.period.start} — {data.period.end} · {data.period.timezone}. {t('hindsight.spend_hint')}</p>
        {#if data.usage.length}<UsageChart rows={data.usage} />{:else}<p class="empty small">{t('hindsight.no_spend_data')}</p>{/if}
      </section>
      <section class="panel stack">
        <h2>{t('hindsight.web')}</h2>
        {#if data.web_url}<div><a class="btn" href={data.web_url} target="_blank" rel="noopener noreferrer">{t('hindsight.open_web')}</a></div>
        {:else}<p class="muted">{t('hindsight.web_unconfigured')}</p>{/if}
      </section>
    {/if}
  </div>
{/if}

<style>
  .sections { gap: var(--sp-14); padding-bottom: var(--sp-20); }
  h2, h3, h4, p { margin: 0; }
  h2 { font-size: var(--fs-title); } h3 { font-size: var(--fs-body); overflow-wrap: anywhere; } h4 { font-size: var(--fs-md); }
  .row { display: flex; align-items: center; gap: var(--sp-8); flex-wrap: wrap; }
  .between { justify-content: space-between; }
  .small { font-size: var(--fs-sm); }
  .wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: var(--fs-md); }
  th, td { padding: var(--sp-10) var(--sp-8); border-bottom: 1px solid var(--line-soft); text-align: left; }
  th { color: var(--muted); } code { font-size: var(--fs-sm); overflow-wrap: anywhere; }
  .bank { border-top: 1px solid var(--line-soft); padding-top: var(--sp-14); }
  .facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: var(--sp-12); margin: 0; }
  dt { color: var(--muted); font-size: var(--fs-sm); } dd { margin: var(--sp-4) 0 0; overflow-wrap: anywhere; }
  .problem { color: var(--crit); font-weight: 600; }
  summary { cursor: pointer; padding: var(--sp-8) 0; }
</style>
