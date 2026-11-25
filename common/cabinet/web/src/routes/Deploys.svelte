<script lang="ts">
  // «Деплой»: a panel per project and a row per environment in the passport's order — the status of its last run,
  // the commit it ran and when, the commit that runs there when the last run is another one, how far it is ahead of
  // prod (`ahead_of`) and how many commits of its branch wait to be deployed there. The columns are fixed: a status
  // or the owner's button never moves the rest. A row opens its details (the branch and its head, who started the
  // run, its inputs and log, the cards waiting for it, the last launch from the cabinet). A project whose read
  // failed says since when there is no fresh data and why. The platform's own release has a panel of its own; the
  // journal of runs is folded under a button, twenty one-line runs at a time, with a project and environment filter.
  // The forge reader brings the runs and the counts every 45 s; the page reads itself every 30 s. The owner starts a
  // manual environment (prod) from its row: the sheet names the commit that goes out and how many new commits it
  // brings, skip_tests where the workflow takes it, then the saved passkey's signature with no fingerprint or PIN —
  // no typed word; kd-review dispatches it with its own token, and the run comes with the reader's next read.
  import { onMount } from "svelte";
  import Freshness from "../components/Freshness.svelte";
  import Sheet from "../components/Sheet.svelte";
  import { apiGet } from "../lib/api";
  import {
    deltas, envsOf, inputsText, JOURNAL_STEP, journalUrl, journalView, launchDeploy, live, PLATFORM, projectRows,
    projectsOf, shortSha, statusClass, statusMark,
  } from "../lib/deploys";
  import { errorText, t, type Key } from "../lib/i18n.svelte";
  import { synced } from "../lib/synced.svelte";
  import { age, when } from "../lib/time";
  import { toast } from "../lib/toast.svelte";
  import type { DeployEnv, DeployList, DeployPage, DeployRun } from "../lib/types";
  import { isCancelled } from "../lib/webauthn";

  let list = $state<DeployList | null>(null);
  let opened = $state("");
  let journalOpen = $state(false);
  let journal = $state<DeployRun[]>([]);
  let next = $state<string | null>(null);
  let wanted = $state(JOURNAL_STEP);
  let project = $state("");
  let env = $state("");
  let loadingMore = $state(false);
  let journalError = $state<unknown>(null);
  let launching = $state<DeployEnv | null>(null);
  let skipTests = $state(false);
  let busy = $state(false);
  let launchError = $state("");

  function openLaunch(row: DeployEnv) {
    launching = row;
    skipTests = false;
    launchError = "";
  }

  async function launch() {
    const row = launching;
    if (!row) return;
    busy = true;
    launchError = "";
    try {
      const done = await launchDeploy(row, row.skip_tests && skipTests);
      toast(t("deploys.launch.done", { project: row.name, env: done.env, sha: shortSha(done.sha) }));
      launching = null;
      await data.run();
    } catch (error) {
      launchError = isCancelled(error) ? t("error.passkey_cancelled") : errorText(error);
    } finally {
      busy = false;
    }
  }

  const projects = $derived(list ? projectsOf(list.envs) : []);
  const rows = $derived(list ? projectRows(list.envs) : []);
  const envChoices = $derived(project && list ? envsOf(list.envs, project) : []);
  const view = $derived(journalView(journal.length, wanted, next !== null));

  function toggle(key: string) {
    opened = opened === key ? "" : key;
  }

  async function readJournal() {
    try {
      const page = await apiGet<DeployPage>(journalUrl(project, env));
      journal = page.items;
      next = page.next;
      journalError = null;
    } catch (error) {
      journalError = error;
    }
  }

  const data = synced(async () => {
    list = await apiGet<DeployList>("/api/deploys");
    if (journalOpen) await readJournal();
  });

  function toggleJournal() {
    journalOpen = !journalOpen;
    if (journalOpen) {
      wanted = JOURNAL_STEP;
      void readJournal();
    }
  }

  async function more() {
    wanted += JOURNAL_STEP;
    if (!journalView(journal.length, wanted, next !== null).fetch || !next) return;
    loadingMore = true;
    try {
      const page = await apiGet<DeployPage>(journalUrl(project, env, next));
      journal = [...journal, ...page.items];
      next = page.next;
    } catch (error) {
      journalError = error;
    } finally {
      loadingMore = false;
    }
  }

  function pickProject(value: string) {
    project = value;
    env = "";
    wanted = JOURNAL_STEP;
    void readJournal();
  }

  function pickEnv(value: string) {
    env = value;
    wanted = JOURNAL_STEP;
    void readJournal();
  }

  const statusText = (status: DeployRun["status"] | undefined) =>
    status ? t(`deploys.status.${status}` as Key) : t("deploys.no_runs");

  function projectName(id: string): string {
    if (id === PLATFORM) return t("deploys.platform");
    return projects.find((p) => p.id === id)?.name ?? id;
  }

  onMount(() => {
    void data.run();
    const tick = setInterval(() => void data.run(), 30_000);
    return () => {
      clearInterval(tick);
      data.dispose();
    };
  });
</script>

{#snippet runLine(run: DeployRun)}
  {#if run.sha}<code title={run.sha}>{shortSha(run.sha)}</code> · {/if}
  <span title={when(run.created)}>{age(run.created)}</span>
  {#if run.actor} · {t("deploys.by", { actor: run.actor })}{/if}
  {#if Object.keys(run.inputs).length} · <code>{inputsText(run.inputs)}</code>{/if}
  {#if run.url} · <a href={run.url} target="_blank" rel="noopener noreferrer">{t("deploys.log")}</a>{/if}
  {#if run.card} · <a href={`/cards/${run.card}`}>{t("deploys.card", { id: run.card })}</a>{/if}
{/snippet}

{#snippet pill(status: DeployRun["status"] | undefined)}
  <span class="pill {statusClass(status)}"><span aria-hidden="true">{statusMark(status)}</span> {statusText(status)}</span>
{/snippet}

{#snippet details(row: DeployEnv)}
  {@const was = live(row)}
  <div class="details">
    <span class="muted">
      <code>{t("deploys.branch", { branch: row.branch })}</code>
      {#if row.head} · {t("deploys.head", { branch: row.branch, sha: shortSha(row.head) })}{/if}
      {#if row.manual} · {t("deploys.manual")}{/if}
    </span>
    {#if row.run}<span class="muted">{statusText(row.run.status)}: {@render runLine(row.run)}</span>{/if}
    {#if was}<span class="muted">{statusText(was.status)}: {@render runLine(was)}</span>{/if}
    {#if row.waiting?.length}
      <span class="muted">
        {t("deploys.waiting")}:
        {#each row.waiting as w, i (w.card)}{#if i}, {/if}<a href={`/cards/${w.card}`} title={w.sha}>{w.card}</a>{/each}
      </span>
    {/if}
    {#if row.launched}
      <span class="muted" class:err={row.launched.outcome !== "dispatched"} title={row.launched.message}>
        {t(row.launched.outcome === "dispatched" ? "deploys.launch.last" : "deploys.launch.last_refused", {
          user: row.launched.user, sha: shortSha(row.launched.sha), age: age(row.launched.at),
        })}{#if row.launched.skip_tests} · {t("deploys.launch.no_tests")}{/if}
      </span>
    {/if}
  </div>
{/snippet}

<div class="topbar">
  <h1>{t("nav.deploys")}</h1>
  <Freshness {data} />
</div>

{#if list === null}
  {#if data.error}<p class="empty" role="alert">{errorText(data.error)}</p>{:else}<p class="empty">{t("app.loading")}</p>{/if}
{:else}
  {#if list.envs.length === 0}<p class="empty">{t("deploys.empty")}</p>{/if}
  <div class="projects">
    {#each rows as group (group.id)}
      <section class="project" aria-labelledby={`deploy-${group.id}`}>
        <h2 id={`deploy-${group.id}`}>{group.name}</h2>
        {#if group.stale || group.error}
          <p class="readfail" role="status">
            <span aria-hidden="true">⚠</span>
            {group.okAt ? t("deploys.no_data", { time: when(group.okAt) }) : t("deploys.no_data_never")}{#if group.error}:
              <span class="why">{group.error}</span>{/if}
          </p>
        {/if}
        <div class="envs" class:launchable={group.launch}>
          {#each group.envs as row (row.env)}
            {@const key = `${row.project}:${row.env}`}
            {@const was = live(row)}
            <div class="erow" class:open={opened === key} class:dim={group.stale}>
              <button type="button" class="ehead" aria-expanded={opened === key} onclick={() => toggle(key)}>
                <span class="ename">{row.env}</span>
                {@render pill(row.run?.status)}
                <span class="what">
                  {#if row.run}
                    <span><code title={row.run.sha}>{shortSha(row.run.sha)}</code>
                      <span class="muted" title={when(row.run.created)}>{age(row.run.created)}</span></span>
                  {/if}
                  {#if was}<span class="muted">{t("deploys.live", { sha: shortSha(was.sha), age: age(was.created) })}</span>{/if}
                  {#each deltas(row) as d (d.key)}<span class="delta {d.tone}">{t(d.key, d.params)}</span>{/each}
                  {#if row.waiting?.length}
                    <span class="delta muted">{t("deploys.waiting_count", { n: row.waiting.length })}</span>
                  {/if}
                </span>
              </button>
              {#if row.launch}
                <span class="act">
                  <button class="btn small primary" type="button" onclick={() => openLaunch(row)}>
                    {t("deploys.launch.button")}
                  </button>
                </span>
              {/if}
              {#if opened === key}{@render details(row)}{/if}
            </div>
          {/each}
        </div>
      </section>
    {/each}
    <section class="project" aria-labelledby="deploy-platform">
      <h2 id="deploy-platform">{t("deploys.platform")}</h2>
      <div class="envs">
        <div class="erow">
          <div class="ehead static">
            <span class="ename">{t("deploys.release")}</span>
            {@render pill(list.platform?.status)}
            <span class="what muted">{#if list.platform}<span>{@render runLine(list.platform)}</span>{/if}</span>
          </div>
        </div>
      </div>
    </section>
  </div>

  <section class="journal">
    <button class="btn small ghost" type="button" aria-expanded={journalOpen} onclick={toggleJournal}>
      {t(journalOpen ? "deploys.journal_hide" : "deploys.journal_show")}
    </button>
    {#if journalOpen}
      <div class="filters">
        <select aria-label={t("deploys.filter_project")} value={project}
                onchange={(e) => pickProject((e.currentTarget as HTMLSelectElement).value)}>
          <option value="">{t("deploys.all_projects")}</option>
          {#each projects as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
          <option value={PLATFORM}>{t("deploys.platform")}</option>
        </select>
        <select aria-label={t("deploys.filter_env")} value={env} disabled={!project}
                onchange={(e) => pickEnv((e.currentTarget as HTMLSelectElement).value)}>
          <option value="">{t("deploys.all_envs")}</option>
          {#each envChoices as e (e)}<option value={e}>{e}</option>{/each}
        </select>
      </div>
      {#if journalError}<p class="empty" role="alert">{errorText(journalError)}</p>{/if}
      {#if journal.length === 0}
        <p class="empty">{t("deploys.journal_empty")}</p>
      {:else}
        <ul class="runs" aria-label={t("deploys.journal")}>
          {#each journal.slice(0, view.shown) as run (`${run.project}:${run.run}`)}
            <li>
              <span class="mark {statusClass(run.status)}" title={statusText(run.status)}>
                <span aria-hidden="true">{statusMark(run.status)}</span>
                <span class="visually-hidden">{statusText(run.status)}</span>
              </span>
              <b>{projectName(run.project)} · {run.env}</b>
              <span class="muted">{@render runLine(run)}</span>
            </li>
          {/each}
        </ul>
        {#if view.more}
          <button class="btn small ghost" type="button" disabled={loadingMore} onclick={() => void more()}>
            {t("deploys.more")}
          </button>
        {/if}
      {/if}
    {/if}
  </section>
{/if}

{#if launching}
  <Sheet title={t("deploys.launch.title", { project: launching.name, env: launching.env })} onclose={() => (launching = null)}>
    <p>
      {launching.head
        ? t("deploys.launch.head", { branch: launching.branch, sha: shortSha(launching.head) })
        : t("deploys.launch.head_unknown", { branch: launching.branch })}
    </p>
    {#if launching.pending !== null}
      <p>
        {launching.pending > 0
          ? t("deploys.launch.pending", {
              env: launching.env, sha: launching.deployed ? shortSha(launching.deployed.sha) : "—", n: launching.pending,
            })
          : t("deploys.launch.nothing", { env: launching.env })}
      </p>
    {/if}
    {#if launching.skip_tests}
      <label class="check">
        <input type="checkbox" bind:checked={skipTests} />
        <span>{t("deploys.launch.skip_tests")}</span>
      </label>
    {/if}
    <p class="muted">{t("deploys.launch.sign_note")}</p>
    {#if launchError}<p class="error-text" role="alert">{launchError}</p>{/if}
    <div class="actions">
      <button type="button" class="btn" disabled={busy} onclick={() => (launching = null)}>{t("action.cancel")}</button>
      <button type="button" class="btn primary" disabled={busy} onclick={() => void launch()}>
        {t("deploys.launch.go")}
      </button>
    </div>
  </Sheet>
{/if}

<style>
  .projects { display: grid; gap: 14px; margin-top: 4px; }
  .project {
    background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden;
  }
  .project h2 { margin: 0; padding: 14px 16px 10px; font-size: 17px; font-weight: 650; line-height: 1.3; }
  .readfail {
    margin: 0 16px 10px; padding: 8px 10px; border-radius: 8px; font-size: 13px; line-height: 1.4;
    color: var(--warn); background: var(--warn-bg); overflow-wrap: anywhere;
  }
  .readfail .why { color: var(--text); }
  /* one grid per panel: the environment, its status, what it runs, the owner's button — the columns never move */
  .envs { display: grid; grid-template-columns: 6.5rem 8rem minmax(0, 1fr); }
  .envs.launchable { grid-template-columns: 6.5rem 8rem minmax(0, 1fr) 8rem; }
  .erow {
    grid-column: 1 / -1; display: grid; grid-template-columns: subgrid; align-items: center;
    border-top: 1px solid var(--line);
  }
  .erow.dim .ehead { opacity: .75; }
  .ehead {
    grid-column: 1 / 4; display: grid; grid-template-columns: subgrid; align-items: center; min-height: 48px;
    padding: 8px 0 8px 16px; background: none; border: 0; color: inherit; font: inherit; text-align: left;
    cursor: pointer;
  }
  .ehead:hover, .erow.open .ehead { background: var(--surface-2); }
  .ehead.static { cursor: default; }
  .ehead.static:hover { background: none; }
  .ename { font-size: 14px; font-weight: 600; overflow-wrap: anywhere; padding-right: 8px; }
  .pill {
    justify-self: start; display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px;
    border-radius: 999px; font-size: 12.5px; font-weight: 500; white-space: nowrap;
  }
  .what {
    display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 12px; min-width: 0; padding-right: 12px;
    font-size: 13px; overflow-wrap: anywhere;
  }
  .delta { font-size: 12.5px; font-weight: 600; white-space: nowrap; }
  .delta.acc { color: var(--accent); }
  .delta.warn { color: var(--warn); }
  .delta.muted { color: var(--muted); font-weight: 400; }
  .act { grid-column: 4; padding-right: 16px; }
  .act .btn { width: 100%; }
  .details {
    grid-column: 1 / -1; display: grid; gap: 3px; padding: 8px 16px 12px; font-size: 12.5px;
    background: var(--surface-2); overflow-wrap: anywhere;
  }
  :is(.pill, .mark).ok { color: var(--good); background: var(--good-bg); }
  :is(.pill, .mark).crit { color: var(--crit); background: var(--crit-bg); }
  :is(.pill, .mark).warn { color: var(--warn); background: var(--warn-bg); }
  :is(.pill, .mark).off { color: var(--muted); background: var(--surface-2); }
  .err { color: var(--crit); }
  .journal { margin-top: 14px; }
  .filters { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0; }
  .runs { list-style: none; margin: 0; padding: 0; }
  .runs li {
    display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 8px; padding: 4px 0; font-size: 12px;
    border-bottom: 1px solid var(--line); overflow-wrap: anywhere;
  }
  .runs li b { font-size: 13px; }
  .mark { display: inline-flex; justify-content: center; min-width: 22px; border-radius: var(--radius); }
  .check { display: flex; gap: 8px; align-items: center; }
  .actions { display: flex; gap: 8px; justify-content: flex-end; }
  /* a phone: the environment and its status on one line, what it runs under the status, the button full width */
  @media (max-width: 599px) {
    .envs, .envs.launchable { grid-template-columns: 5.5rem minmax(0, 1fr); }
    .ehead { grid-column: 1 / -1; row-gap: 4px; padding-right: 16px; }
    .what { grid-column: 2; padding-right: 0; }
    .act { grid-column: 1 / -1; padding: 0 16px 12px; }
  }
</style>
