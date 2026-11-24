<script lang="ts">
  // The projects' deploys: each environment's last run — its status, commit, time, who started it and the log —
  // and the platform's last release; below them the journal of runs with a project and environment filter.
  // The forge reader brings the runs every 45 s; the page reads itself every 30 s, and an environment whose
  // project was not read well for two minutes says «stale» with the time of its last good read. Under an
  // environment — the cards whose deploy wait names it. The owner starts a manual environment (prod) from here:
  // the word «deploy» typed, skip_tests where the workflow takes it, the saved passkey's signature with no
  // fingerprint or PIN; kd-review dispatches it with its own token, and the run comes with the reader's next read.
  import { onMount } from "svelte";
  import Freshness from "../components/Freshness.svelte";
  import Sheet from "../components/Sheet.svelte";
  import { apiGet } from "../lib/api";
  import {
    CONFIRM_WORD, envsOf, inputsText, journalUrl, launchDeploy, PLATFORM, projectsOf, shortSha, statusClass,
  } from "../lib/deploys";
  import { errorText, t, type Key } from "../lib/i18n.svelte";
  import { synced } from "../lib/synced.svelte";
  import { age, when } from "../lib/time";
  import { toast } from "../lib/toast.svelte";
  import type { DeployEnv, DeployList, DeployPage, DeployRun } from "../lib/types";
  import { isCancelled } from "../lib/webauthn";

  let list = $state<DeployList | null>(null);
  let journal = $state<DeployRun[]>([]);
  let next = $state<string | null>(null);
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
  const envChoices = $derived(project && list ? envsOf(list.envs, project) : []);
  const groups = $derived(
    projects.map((p) => ({ ...p, envs: (list?.envs ?? []).filter((e) => e.project === p.id) })),
  );

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
    await readJournal();
  });

  async function more() {
    if (!next) return;
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
    void readJournal();
  }

  function pickEnv(value: string) {
    env = value;
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
  <span class="muted line">
    {#if run.sha}<code title={run.sha}>{shortSha(run.sha)}</code> · {/if}
    <span title={when(run.created)}>{age(run.created)}</span>
    {#if run.actor} · {t("deploys.by", { actor: run.actor })}{/if}
    {#if Object.keys(run.inputs).length} · <code>{inputsText(run.inputs)}</code>{/if}
    {#if run.url} · <a href={run.url} target="_blank" rel="noopener noreferrer">{t("deploys.log")}</a>{/if}
    {#if run.card} · <a href={`/cards/${run.card}`}>{t("deploys.card", { id: run.card })}</a>{/if}
  </span>
{/snippet}

{#snippet envRow(row: DeployEnv)}
  <div class="item" class:dim={row.stale}>
    <span class="chip text level {statusClass(row.run?.status)}">{statusText(row.run?.status)}</span>
    <span class="body">
      <b>{row.env}</b>
      <span class="muted line">
        <code>{row.branch}</code>{#if row.manual} · {t("deploys.manual")}{/if}
      </span>
      {#if row.run}{@render runLine(row.run)}{/if}
      {#if row.waiting?.length}
        <span class="muted line">
          {t("deploys.waiting")}:
          {#each row.waiting as w, i (w.card)}{#if i}, {/if}<a href={`/cards/${w.card}`} title={w.sha}>{w.card}</a>{/each}
        </span>
      {/if}
      {#if row.error}<span class="line err">{t("deploys.read_error", { error: row.error })}</span>{/if}
      {#if row.launched}
        <span class="muted line" class:err={row.launched.outcome !== "dispatched"} title={row.launched.message}>
          {t(row.launched.outcome === "dispatched" ? "deploys.launch.last" : "deploys.launch.last_refused", {
            user: row.launched.user, sha: shortSha(row.launched.sha), age: age(row.launched.at),
          })}{#if row.launched.skip_tests} · {t("deploys.launch.no_tests")}{/if}
        </span>
      {/if}
    </span>
    <span class="side">
      {#if row.stale}
        <span class="chip warn" title={t("deploys.stale_title", { time: row.ok_at ? when(row.ok_at) : "—" })}>
          {t("deploys.stale")}
        </span>
      {/if}
      {#if row.launch}
        <button class="btn small primary" type="button" onclick={() => openLaunch(row)}>{t("deploys.launch.button")}</button>
      {/if}
    </span>
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
  {#each groups as group (group.id)}
    <section class="group">
      <h2>{group.name}</h2>
      <div class="list">
        {#each group.envs as row (row.env)}{@render envRow(row)}{/each}
      </div>
    </section>
  {/each}
  <section class="group">
    <h2>{t("deploys.platform")}</h2>
    <div class="list">
      <div class="item">
        <span class="chip text level {statusClass(list.platform?.status)}">{statusText(list.platform?.status)}</span>
        <span class="body">
          <b>{t("deploys.release")}</b>
          {#if list.platform}{@render runLine(list.platform)}{/if}
        </span>
      </div>
    </div>
  </section>

  <section class="group">
    <h2>{t("deploys.journal")}</h2>
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
      <div class="list">
        {#each journal as run (`${run.project}:${run.run}`)}
          <div class="item">
            <span class="chip text level {statusClass(run.status)}">{statusText(run.status)}</span>
            <span class="body">
              <b>{projectName(run.project)} · {run.env}</b>
              {@render runLine(run)}
            </span>
          </div>
        {/each}
      </div>
      {#if next}
        <button class="btn small ghost" type="button" disabled={loadingMore} onclick={() => void more()}>
          {t("deploys.more")}
        </button>
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
  .group { margin-top: 16px; }
  .group h2 { font-size: 15px; margin: 0 0 8px; }
  .list { display: flex; flex-direction: column; gap: 8px; }
  .filters { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
  .item {
    display: grid; grid-template-columns: minmax(0, 128px) minmax(0, 1fr) auto; gap: 10px; align-items: center;
    padding: 12px; background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
  }
  .item.dim { opacity: .7; }
  .body { display: grid; gap: 2px; min-width: 0; }
  .line { font-size: 12px; overflow-wrap: anywhere; }
  .err { color: var(--crit); }
  .side { display: flex; flex-direction: column; align-items: flex-end; gap: 6px; }
  .check { display: flex; gap: 8px; align-items: center; }
  .actions { display: flex; gap: 8px; justify-content: flex-end; }
  .level { justify-content: center; text-align: center; min-width: 0; }
  .level.crit { color: var(--crit); background: var(--crit-bg); }
  .level.warn, .chip.warn { color: var(--warn); background: var(--warn-bg); }
  .level.ok { color: var(--good); background: var(--good-bg); }
  .level.off { color: var(--muted); }
  @media (max-width: 599px) {
    .item { grid-template-columns: minmax(0, 1fr); align-items: start; }
    .level, .item > .chip { justify-self: start; }
  }
</style>
