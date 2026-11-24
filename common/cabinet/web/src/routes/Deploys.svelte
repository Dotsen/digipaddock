<script lang="ts">
  // The projects' deploys, compact: one row per project with a chip per environment — its last run's status — and
  // the platform's last release; a chip opens the run's commit, time, who started it, the log and the cards waiting
  // for it. The journal of runs is folded away under a button and shows twenty one-line runs at a time, with a
  // project and environment filter. The forge reader brings the runs every 45 s; the page reads itself every 30 s,
  // and an environment whose project was not read well for two minutes says «stale» with the time of its last good
  // read. The owner starts a manual environment (prod) from its row: the word «deploy» typed, skip_tests where the
  // workflow takes it, the saved passkey's signature with no fingerprint or PIN; kd-review dispatches it with its
  // own token, and the run comes with the reader's next read.
  import { onMount } from "svelte";
  import Freshness from "../components/Freshness.svelte";
  import Sheet from "../components/Sheet.svelte";
  import { apiGet } from "../lib/api";
  import {
    CONFIRM_WORD, envsOf, inputsText, JOURNAL_STEP, journalUrl, journalView, launchDeploy, PLATFORM, projectRows,
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
  let word = $state("");
  let skipTests = $state(false);
  let busy = $state(false);
  let launchError = $state("");

  function openLaunch(row: DeployEnv) {
    launching = row;
    word = "";
    skipTests = false;
    launchError = "";
  }

  async function launch() {
    const row = launching;
    if (!row || word !== CONFIRM_WORD) return;
    busy = true;
    launchError = "";
    try {
      const done = await launchDeploy(row, word, row.skip_tests && skipTests);
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
  const openedEnv = $derived((list?.envs ?? []).find((e) => `${e.project}:${e.env}` === opened) ?? null);

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

{#snippet chip(key: string, label: string, status: DeployRun["status"] | undefined, stale: boolean, waiting: number)}
  <button type="button" class="env level {statusClass(status)}" class:dim={stale} aria-expanded={opened === key}
          aria-label={t("deploys.env_label", { env: label, status: statusText(status) })}
          title={statusText(status)} onclick={() => toggle(key)}>
    <span aria-hidden="true">{statusMark(status)}</span> {label}{#if waiting}<span class="count">{waiting}</span>{/if}
  </button>
{/snippet}

{#snippet details(row: DeployEnv)}
  <div class="details">
    <span class="muted">
      <b>{row.name} · {row.env}</b> · <code>{t("deploys.branch", { branch: row.branch })}</code>
      {#if row.manual} · {t("deploys.manual")}{/if} · {statusText(row.run?.status)}
    </span>
    {#if row.run}<span class="muted">{@render runLine(row.run)}</span>{/if}
    {#if row.waiting?.length}
      <span class="muted">
        {t("deploys.waiting")}:
        {#each row.waiting as w, i (w.card)}{#if i}, {/if}<a href={`/cards/${w.card}`} title={w.sha}>{w.card}</a>{/each}
      </span>
    {/if}
    {#if row.stale}
      <span class="warn-text">{t("deploys.stale_title", { time: row.ok_at ? when(row.ok_at) : "—" })}</span>
    {/if}
    {#if row.error}<span class="err">{t("deploys.read_error", { error: row.error })}</span>{/if}
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
  <div class="table">
    {#each rows as group (group.id)}
      <div class="prow">
        <b class="name">{group.name}</b>
        <div class="envs">
          {#each group.envs as row (row.env)}
            <span class="pair">
              {@render chip(`${row.project}:${row.env}`, row.env, row.run?.status, row.stale, row.waiting?.length ?? 0)}
              {#if row.launch}
                <button class="btn small primary" type="button" onclick={() => openLaunch(row)}>
                  {t("deploys.launch.button")}
                </button>
              {/if}
            </span>
          {/each}
        </div>
        {#if group.stale}
          <span class="chip warn">{t("deploys.stale")}</span>
        {/if}
      </div>
      {#if openedEnv && openedEnv.project === group.id}{@render details(openedEnv)}{/if}
    {/each}
    <div class="prow">
      <b class="name">{t("deploys.platform")}</b>
      <div class="envs">
        {@render chip(PLATFORM, t("deploys.release"), list.platform?.status, false, 0)}
        {#if list.platform}<span class="muted line">{@render runLine(list.platform)}</span>{/if}
      </div>
    </div>
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
              <span class="mark level {statusClass(run.status)}" title={statusText(run.status)}>
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
    <p>{t("deploys.launch.what", { branch: launching.branch })}</p>
    <label class="field">
      <span>{t("deploys.launch.type", { word: CONFIRM_WORD })}</span>
      <input type="text" autocomplete="off" autocapitalize="off" spellcheck="false" bind:value={word} />
    </label>
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
      <button type="button" class="btn primary" disabled={busy || word !== CONFIRM_WORD} onclick={() => void launch()}>
        {t("deploys.launch.sign")}
      </button>
    </div>
  </Sheet>
{/if}

<style>
  .table {
    margin-top: 12px; background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
  }
  .prow {
    display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; padding: 6px 12px;
    border-top: 1px solid var(--line);
  }
  .prow:first-child { border-top: 0; }
  .name { min-width: 96px; font-size: 14px; }
  .envs { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; flex: 1; min-width: 0; }
  .pair { display: inline-flex; align-items: center; gap: 4px; }
  .env {
    display: inline-flex; align-items: center; gap: 4px; min-height: 40px; padding: 0 10px;
    border: 1px solid var(--line); border-radius: var(--radius); font: inherit; font-size: 13px; cursor: pointer;
  }
  .env[aria-expanded="true"] { border-color: currentColor; }
  .env.dim { opacity: .7; }
  .count {
    margin-left: 2px; padding: 0 5px; border-radius: 8px; font-size: 11px;
    background: var(--surface); color: var(--muted);
  }
  .details {
    display: grid; gap: 2px; padding: 6px 12px 8px; font-size: 12px; overflow-wrap: anywhere;
    border-top: 1px dashed var(--line);
  }
  .line { font-size: 12px; overflow-wrap: anywhere; }
  .journal { margin-top: 12px; }
  .filters { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0; }
  .runs { list-style: none; margin: 0; padding: 0; }
  .runs li {
    display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 8px; padding: 4px 0; font-size: 12px;
    border-bottom: 1px solid var(--line); overflow-wrap: anywhere;
  }
  .runs li b { font-size: 13px; }
  .mark { display: inline-flex; justify-content: center; min-width: 22px; border-radius: var(--radius); }
  .err { color: var(--crit); }
  .warn-text { color: var(--warn); }
  .check { display: flex; gap: 8px; align-items: center; }
  .actions { display: flex; gap: 8px; justify-content: flex-end; }
  .level.crit { color: var(--crit); background: var(--crit-bg); }
  .level.warn, .chip.warn { color: var(--warn); background: var(--warn-bg); }
  .level.ok { color: var(--good); background: var(--good-bg); }
  .level.off { color: var(--muted); background: var(--surface); }
  @media (max-width: 599px) {
    .name { min-width: 0; flex-basis: 100%; }
  }
</style>
