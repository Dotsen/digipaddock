<script lang="ts">
  // The monitor's settings over config.json: each key with its default and the value in force, "Reset"
  // for an edit, the stale mark of an edit the release no longer applies; per server its MB thresholds and the
  // checks switched off. Only `settings` edits; the cabinet itself asks a step-up for a save that weakens the
  // monitor (one signature for the whole save), so the page just retries with one when it answers 403.
  import { onMount } from "svelte";
  import BackButton from "../components/BackButton.svelte";
  import Freshness from "../components/Freshness.svelte";
  import { ApiError, apiGet, apiPost, apiPut } from "../lib/api";
  import { errorText, t, type Key } from "../lib/i18n.svelte";
  import {
    changesOf,
    defaultOf,
    edited,
    effective,
    fieldId,
    formatValue,
    OFF,
    SECTIONS,
    sectionOf,
    toggled,
    type CatalogEntry,
    type Change,
    type MonitorSettings,
  } from "../lib/monitorSettings";
  import { router } from "../lib/router.svelte";
  import { session } from "../lib/session.svelte";
  import { confirmed } from "../lib/stepup-dialog.svelte";
  import { synced } from "../lib/synced.svelte";
  import { when } from "../lib/time";
  import { toast } from "../lib/toast.svelte";
  import { isCancelled } from "../lib/webauthn";

  let page = $state<MonitorSettings | null>(null);
  let texts = $state<Record<string, string>>({});
  let server = $state(router.current.query.get("server") ?? "");
  // each server's switched-off checks as the page holds them; saved together with the fields
  let offBy = $state<Record<string, string[]>>({});
  let busy = $state(false);
  // the owner's test critical alert: its last state and the button
  type Drill = { id: string; state: string; created: string; started: string; finished: string; detail: string };
  let drill = $state<Drill | null>(null);
  let drilling = $state(false);

  // a re-read (back online, the tab back, a reset of one key) keeps what the owner typed: a field is refilled only
  // while it still shows the value in force it was filled with; a full save refills every field
  const data = synced(async () => {
    const before = page;
    page = await apiGet<MonitorSettings>("/api/monitor-settings");
    refill(before, page);
    // the test's state never holds the settings back: a failed read just hides it
    drill = await apiGet<{ test: Drill | null }>("/api/monitor-test").then((r) => r.test, () => null);
  });

  const drillActive = $derived(drill?.state === "queued" || drill?.state === "running");
  const drillWhen = (d: Drill) => when(d.finished || d.started || d.created);

  async function runDrill() {
    drilling = true;
    try {
      const got = await apiPost<{ test: Drill; repeat: boolean }>("/api/monitor-test");
      drill = got.test;
      toast(t(got.repeat ? "mtest.repeat" : "mtest.started"), "info");
    } catch (exc) {
      toast(errorText(exc), "error");
    } finally {
      drilling = false;
    }
  }

  onMount(() => {
    void data.run();
    return () => data.dispose();
  });

  const canEdit = $derived(session.can("settings"));
  const catalog = $derived(page?.catalog ?? []);
  const globals = $derived(catalog.filter((e) => e.scope === "global"));
  const serverNumbers = $derived(catalog.filter((e) => e.scope === "server" && e.key !== OFF));
  const offEntry = $derived(catalog.find((e) => e.key === OFF));

  const offOf = (s: MonitorSettings, alias: string): string[] =>
    [...((effective(s, OFF, alias) as string[] | undefined) ?? [])];
  const sameSet = (a: string[], b: string[]) => JSON.stringify([...a].sort()) === JSON.stringify([...b].sort());

  /** The fields of the globals and of `alias` from `next`; a field the owner changed since `before` stays. */
  function fill(before: MonitorSettings | null, next: MonitorSettings, alias: string, keep: boolean) {
    for (const e of next.catalog ?? []) {
      if (e.scope === "server" && (!alias || e.key === OFF)) continue;
      const where = e.scope === "server" ? alias : "";
      const id = fieldId(e.key, where);
      const shownBefore = before ? formatValue(e, effective(before, e.key, where)) : undefined;
      if (!keep || texts[id] === undefined || texts[id] === shownBefore) {
        texts[id] = formatValue(e, effective(next, e.key, where));
      }
    }
    if (alias && (!keep || !offBy[alias] || (before && sameSet(offBy[alias], offOf(before, alias))))) {
      offBy[alias] = offOf(next, alias);
    }
  }

  function refill(before: MonitorSettings | null, next: MonitorSettings, keep = true) {
    for (const alias of new Set(["", server, ...Object.keys(offBy)])) fill(before, next, alias, keep);
  }

  function pick(alias: string) {
    server = alias;
    if (page && alias) fill(page, page, alias, true); // the globals and another server's fields stay as typed
  }

  const label = (key: string) => t(`msettings.key.${key}` as Key);
  const shown = (e: CatalogEntry, value: unknown) => formatValue(e, value) || t("msettings.none");

  /** Send `changes`; `whole` — the whole form was saved: every field is refilled from the new set. */
  async function send(changes: Change[], whole: boolean) {
    if (!page || !changes.length) {
      toast(t("msettings.nothing"), "info");
      return;
    }
    busy = true;
    try {
      const revision = page.revision;
      await confirmed("monitor.settings", (stepup) =>
        apiPut("/api/monitor-settings", stepup ? { revision, changes, stepup } : { revision, changes }),
      );
      toast(t("msettings.saved"), "info");
      if (whole) {
        const before = page;
        page = await apiGet<MonitorSettings>("/api/monitor-settings");
        refill(before, page, false);
      } else await data.run();
    } catch (exc) {
      if (exc instanceof ApiError && exc.kind === "conflict") {
        toast(t("msettings.conflict"), "error");
        await data.run();
      } else if (exc instanceof ApiError && exc.kind === "invalid") {
        toast(`${errorText(exc)}: ${exc.message}`, "error"); // which key and why: the pairs, a server's checks
      } else {
        toast(isCancelled(exc) ? t("error.passkey_cancelled") : errorText(exc), "error");
      }
    } finally {
      busy = false;
    }
  }

  function save() {
    if (!page) return;
    const { changes, bad } = changesOf(page, texts);
    if (bad.length) {
      toast(t("msettings.bad", { keys: bad.map((id) => label(id.split("|")[0])).join(", ") }), "error");
      return;
    }
    if (offEntry) {
      for (const [alias, list] of Object.entries(offBy)) {
        if (!sameSet(list, offOf(page, alias))) changes.push({ key: OFF, server: alias, value: list });
      }
    }
    void send(changes, true);
  }

  const resetKey = (key: string, alias = "") =>
    void send([alias ? { key, server: alias, reset: true } : { key, reset: true }], false);
</script>

<div class="topbar">
  <BackButton />
  <h1>{t("msettings.title")}</h1>
  <Freshness {data} />
  <span class="spacer"></span>
  {#if page && canEdit}
    <button class="btn primary" type="button" disabled={busy} onclick={save}>{t("msettings.save")}</button>
  {/if}
</div>

{#if !page}
  {#if data.error}<p class="empty" role="alert">{errorText(data.error)}</p>{:else}<p class="empty">{t("app.loading")}</p>{/if}
{:else}
  <p class="muted intro">{t("msettings.intro")}</p>
  <p class="muted intro">{t("mon.quiet_timezone", {timezone:page.timezone ?? "UTC"})}</p>
  {#if !canEdit}<p class="muted intro">{t("msettings.readonly")}</p>{/if}

  <section class="drill">
    <h2>{t("mtest.title")}</h2>
    <p class="muted intro">{t("mtest.help")}</p>
    {#if drill}
      <p class="intro">
        {t(`mtest.state.${drill.state}` as Key, {
          when: drillWhen(drill),
          detail: drill.detail === "timeout" ? t("mtest.detail.timeout") : drill.detail,
        })}
      </p>
    {/if}
    {#if canEdit}
      <button class="btn" type="button" disabled={drilling || drillActive} onclick={runDrill}>{t("mtest.run")}</button>
    {/if}
  </section>

  {#snippet row(s: MonitorSettings, e: CatalogEntry, alias: string)}
    {@const id = fieldId(e.key, alias)}
    {@const edit = edited(s, e.key, alias)}
    <div class="row" class:changed={!!edit}>
      <label class="name" for={id}>{label(e.key)}</label>
      <input
        {id}
        type="text"
        inputmode={e.type === "int" || e.type === "num" ? "decimal" : "text"}
        disabled={!canEdit || busy}
        bind:value={texts[id]}
      />
      <span class="muted meta">
        {t("msettings.default", { value: shown(e, defaultOf(s, e.key, alias)) })}
        {#if edit} · <span title={edit.by}>{t("msettings.edited", { when: when(edit.at) })}</span>{/if}
        {#if edit?.stale} · <span class="stale">{t("msettings.stale")}</span>{/if}
      </span>
      {#if edit && canEdit}
        <button class="btn small ghost" type="button" disabled={busy} onclick={() => resetKey(e.key, alias)}>
          {t("msettings.reset")}
        </button>
      {/if}
    </div>
  {/snippet}

  <section>
    <h2>{t("msettings.section.server")}</h2>
    <label class="field pick">
      <span>{t("msettings.server")}</span>
      <select value={server} onchange={(ev) => pick((ev.currentTarget as HTMLSelectElement).value)}>
        <option value="">{t("msettings.server.pick")}</option>
        {#each page.servers ?? [] as alias (alias)}<option value={alias}>{alias}</option>{/each}
      </select>
    </label>
    {#if server}
      {#each serverNumbers as e (e.key)}{@render row(page, e, server)}{/each}
      {#if offEntry}
        {@const runs = page.checks?.[server] ?? []}
        {@const offEdit = edited(page, OFF, server)}
        <h3>{t("msettings.checks")}</h3>
        <p class="muted intro">{t("mon.disable_help")}</p>
        {#if runs.length}
          <div class="checks">
            {#each runs as check (check)}
              <label class="check">
                <input
                  type="checkbox"
                  checked={!(offBy[server] ?? []).includes(check)}
                  disabled={!canEdit || busy}
                  onchange={(ev) => (offBy[server] = toggled(offBy[server] ?? [], check, (ev.currentTarget as HTMLInputElement).checked))}
                />
                {t(`msettings.check.${check}` as Key)}
              </label>
            {/each}
          </div>
          {#if offEdit && canEdit}
            <button class="btn small ghost" type="button" disabled={busy} onclick={() => resetKey(OFF, server)}>
              {t("msettings.reset")}
            </button>
          {/if}
        {:else}<p class="empty">{t("msettings.checks.none")}</p>{/if}
      {/if}
    {/if}
  </section>

  <h2>{t("mon.global_settings")}</h2>
  {#each SECTIONS as section (section)}
    {@const entries = globals.filter((e) => sectionOf(e.key) === section)}
    {#if entries.length}
      <section>
        <h2>{t(`msettings.section.${section}` as Key)}</h2>
        {#each entries as e (e.key)}{@render row(page, e, "")}{/each}
      </section>
    {/if}
  {/each}


{/if}

<style>
  .intro { font-size: var(--fs-md); margin: 0 0 var(--sp-8); }
  section { margin: var(--sp-16) 0; }
  h2 { font-size: var(--fs-body); margin: 0 0 var(--sp-8); }
  h3 { font-size: var(--fs-md); margin: var(--sp-12) 0 var(--sp-6); }
  .row {
    display: grid; grid-template-columns: minmax(0, 1fr) 180px; gap: var(--sp-4) var(--sp-12); align-items: center;
    padding: var(--sp-8) 0; border-bottom: 1px solid var(--line-soft); font-size: var(--fs-md);
  }
  .row.changed .name { font-weight: 600; }
  .row .meta { grid-column: 1; font-size: var(--fs-sm); }
  .row .btn { grid-column: 2; justify-self: start; }
  .row input { width: 100%; }
  .stale { color: var(--warn); }
  .pick { max-width: 320px; }
  .checks { display: grid; gap: var(--sp-6); grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); font-size: var(--fs-md); }
  .check { display: flex; gap: var(--sp-6); align-items: center; }
  .empty { margin: 0; padding: var(--sp-8) 0; font-size: var(--fs-md); }
  @media (max-width: 599px) { .row { grid-template-columns: 1fr; } .row .meta, .row .btn { grid-column: 1; } }
</style>
