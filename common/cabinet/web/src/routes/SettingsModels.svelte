<script lang="ts">
  // Models of other providers, with `settings`: the list — each with its kind, the provider's model id,
  // prices, whether the kind's key is on the host (and how root puts it there), the last probe's outcome, an
  // enable switch and "Check" (one short run through the session host); a new model (its id never changes:
  // the usage ledger counts under it); the prices and the title editable in place; a delete that a card's or a
  // project's reference refuses — disable the model instead. the models of the Claude subscription come
  // first — rows of the catalog: probed like any, never changed, disabled or deleted. three tabs — the
  // models, the providers' keys (the
  // owner's, signed with a passkey) and a new model; the tab is in the address (`?tab=`), a model without its key links
  // to the keys.
  import { onMount } from "svelte";
  import BackButton from "../components/BackButton.svelte";
  import ProviderKey from "../components/ProviderKey.svelte";
  import { apiDelete, apiGet, apiPatch, apiPost } from "../lib/api";
  import { errorText, t, type Key } from "../lib/i18n.svelte";
  import { navigate, router } from "../lib/router.svelte";
  import { session } from "../lib/session.svelte";
  import { models } from "../lib/stores/models.svelte";
  import { toast } from "../lib/toast.svelte";
  import { when } from "../lib/time";
  import type { LlmModel, LlmProvider } from "../lib/types";

  const settings = $derived(session.can("settings"));
  const ID_PATTERN = "[a-z0-9][a-z0-9._-]{0,63}";
  const PRICES = ["price_in", "price_out", "price_cache_read", "price_cache_write"] as const;
  type Price = (typeof PRICES)[number];

  let providers = $state<LlmProvider[]>([]);
  let subscription = $state<LlmProvider | null>(null);
  const keyed = $derived([...(subscription ? [subscription] : []), ...providers.filter((p) => p.manage_key)]);
  const TABS = ["list", "keys", "add"] as const;
  type Tab = (typeof TABS)[number];
  const TAB_LABEL: Record<Tab, Key> = { list: "models.tab_list", keys: "models.keys", add: "models.add" };
  const tabs = $derived(TABS.filter((name) => name !== "keys" || keyed.length > 0));
  const asked = $derived(router.current.query.get("tab") ?? "");
  const tab = $derived<Tab>((tabs as readonly string[]).includes(asked) ? (asked as Tab) : "list");
  /** A tab is a place in the address: a link opens it, «back» leaves the page, not the tab. */
  const show = (next: Tab) => navigate(next === "list" ? "/settings/models" : `/settings/models?tab=${next}`, { replace: true });
  let listed = $state(false);
  let busy = $state("");

  // the new model
  let id = $state("");
  let provider = $state("");
  let upstream = $state("");
  let baseUrl = $state("");
  let title = $state("");
  let prices = $state<Record<Price, string>>({ price_in: "", price_out: "", price_cache_read: "", price_cache_write: "" });
  let context = $state("");

  // one model opened for a change
  let open = $state("");
  let editTitle = $state("");
  let editUpstream = $state("");
  let editBaseUrl = $state("");
  let editPrices = $state<Record<Price, string>>({ price_in: "", price_out: "", price_cache_read: "", price_cache_write: "" });
  let editContext = $state("");

  const kind = $derived(providers.find((p) => p.id === provider));
  const custom = (p: string) => providers.find((k) => k.id === p)?.base_url === "";
  // a number input's bound value is a number (or null) once typed into, a string before
  const number = (value: unknown) => (typeof value === "number" ? value : String(value ?? "").trim() === "" ? NaN : Number(value));
  const valid = (value: string) => Number.isFinite(number(value)) && number(value) >= 0;
  const ready = $derived(
    new RegExp(`^${ID_PATTERN}$`).test(id) && !!provider && upstream.trim() !== "" && title.trim() !== "" &&
      PRICES.every((k) => valid(prices[k])) && Number.isInteger(number(context)) && number(context) >= 1000 &&
      (!custom(provider) || /^https:\/\/\S+$/.test(baseUrl.trim())),
  );
  const failed = (exc: unknown) => toast(errorText(exc), "error");
  async function keysChanged() {
    providers = await apiGet<LlmProvider[]>("/api/models/providers");
    if (providers.some((p) => p.manage_key)) {
      const found = await apiGet<Partial<LlmProvider>>("/api/model-credentials/subscription");
      subscription = { id: "claude-subscription", title: t("models.subscription"), base_url: "", hint: "", caching: false,
        key: false, ...found, key_type: "claude_subscription", replace_only: true, manage_key: true };
    }
    await models.refresh();
  }
  const providerTitle = (p: string) => providers.find((k) => k.id === p)?.title ?? p;

  function probeText(model: LlmModel): string {
    const probe = model.probe ?? {};
    if (probe.state === "requested") return t("models.probe_requested");
    if (probe.state === "running") return t("models.probe_running");
    if (probe.ok === true) return t("models.probe_ok", { model: probe.model || model.upstream || model.id, ms: probe.ms ?? 0 });
    if (probe.ok === false) return t("models.probe_failed", { detail: probe.detail || "" });
    return "";
  }

  onMount(() => {
    if (!settings) return;
    models.ensure().then(() => (listed = true), (exc) => {
      listed = true; // the toast says why the list is empty
      failed(exc);
    });
    apiGet<LlmProvider[]>("/api/models/providers").then((found) => {
      providers = found;
      provider ||= found[0]?.id ?? "";
      if (found.some((p) => p.manage_key)) void keysChanged().catch(failed);
    }, failed);
    const poll = setInterval(() => {
      if (subscription?.probe?.state) void keysChanged().catch(failed);
    }, 2000);
    return () => clearInterval(poll);
  });

  async function run(step: string, work: () => Promise<unknown>) {
    busy = step;
    try {
      await work();
    } catch (exc) {
      failed(exc);
    } finally {
      busy = "";
    }
  }

  const create = (event: SubmitEvent) => {
    event.preventDefault();
    return run("create", async () => {
      const made = await apiPost<LlmModel>("/api/models", {
        id, provider, upstream: upstream.trim(), base_url: custom(provider) ? baseUrl.trim() : "", title: title.trim(),
        ...Object.fromEntries(PRICES.map((k) => [k, number(prices[k])])), context: number(context),
      });
      id = upstream = baseUrl = title = context = "";
      prices = { price_in: "", price_out: "", price_cache_read: "", price_cache_write: "" };
      await models.refresh();
      toast(t("models.created", { id: made.id }), "info");
      show("list");
    });
  };

  function edit(model: LlmModel) {
    if (open === model.id) return (open = "");
    open = model.id;
    editTitle = model.title;
    editUpstream = model.upstream ?? "";
    editBaseUrl = model.base_url ?? "";
    editPrices = Object.fromEntries(PRICES.map((k) => [k, String(model[k] ?? "")])) as Record<Price, string>;
    editContext = String(model.context ?? "");
  }

  const save = (model: LlmModel) =>
    run(`save-${model.id}`, async () => {
      const change: Record<string, string | number> = {};
      if (editTitle.trim() !== model.title) change.title = editTitle.trim();
      if (editUpstream.trim() !== (model.upstream ?? "")) change.upstream = editUpstream.trim();
      if (custom(model.provider) && editBaseUrl.trim() !== (model.base_url ?? "")) change.base_url = editBaseUrl.trim();
      for (const k of PRICES) if (number(editPrices[k]) !== model[k]) change[k] = number(editPrices[k]);
      if (number(editContext) !== model.context) change.context = number(editContext);
      if (Object.keys(change).length) {
        models.put(await apiPatch<LlmModel>(`/api/models/${encodeURIComponent(model.id)}`, change));
        toast(t("settings.saved"), "info");
      }
      open = "";
    });

  // the answer is the row as it is now: shown at once, the model event reads the list again anyway
  const toggle = (model: LlmModel) =>
    run(`enable-${model.id}`, async () => {
      models.put(await apiPatch<LlmModel>(`/api/models/${encodeURIComponent(model.id)}`, { enabled: model.enabled ? 0 : 1 }));
    });

  const probe = (model: LlmModel) =>
    run(`probe-${model.id}`, async () => {
      models.put(await apiPost<LlmModel>(`/api/models/${encodeURIComponent(model.id)}/probe`, {}));
    });

  const remove = (model: LlmModel) =>
    run(`delete-${model.id}`, async () => {
      if (!confirm(t("models.delete_confirm", { id: model.id }))) return;
      await apiDelete(`/api/models/${encodeURIComponent(model.id)}`);
      await models.refresh();
      toast(t("models.deleted", { id: model.id }), "info");
    });
</script>

<div class="topbar">
  <a class="back" href="/settings" aria-label={t("nav.settings")}>←</a>
  <BackButton />
  <h1>{t("settings.models")}</h1>
</div>

{#if !settings}
  <p class="empty">{t("error.forbidden")}</p>
{:else}
  <p class="muted small intro">{t("models.intro")}</p>
  <div class="tabs" role="tablist">
    {#each tabs as name (name)}
      <button type="button" role="tab" aria-selected={tab === name} class:on={tab === name} onclick={() => show(name)}>{t(TAB_LABEL[name])}</button>
    {/each}
  </div>
  {#if tab === "keys"}
    <p class="muted small intro">{t("models.key_help")}</p>
    <div class="keys">
      {#each keyed as p (p.id)}
        <ProviderKey provider={p} changed={keysChanged} />
      {/each}
    </div>
  {:else if tab === "list"}
  <ul class="models">
    {#each models.list as model (model.id)}
      <li class="panel" class:off={!model.enabled}>
        <div class="head">
          <span class="who">
            <b>{model.title || model.id}</b>
            {#if model.builtin}
              <span class="muted small"><code>{model.id}</code>{#if model.version}{" · "}{t("models.version", { model: model.version })}{/if} · {t("models.builtin_hint")}</span>
              {#if !model.key}<span class="small warn">{t("models.builtin_no_token")}</span>{/if}
            {:else}
            <span class="muted small"><code>{model.id}</code> · {providerTitle(model.provider)} · {model.upstream}{#if model.base_url}{" · "}{model.base_url}{/if}</span>
            <span class="small">
              <span class:warn={!model.key}>{t(model.key ? "models.key_present" : "models.key_missing")}</span>
              {#if model.experimental}<span class="chip acc text" title={t("models.experimental_hint")}>{t("models.experimental")}</span>{/if}
            </span>
            {#if !model.key && keyed.some((p) => p.id === model.provider)}
              <a class="small" href="/settings/models?tab=keys" onclick={(e) => { e.preventDefault(); show("keys"); }}>{t("models.key_add")}</a>
            {:else if !model.key}<span class="small muted">{t("models.key_hint", { provider: model.provider })}</span>{/if}
            {/if}
            {#if probeText(model)}
              <span class="small probe">
                <span class:warn={model.probe?.ok === false} class:good={model.probe?.ok === true}>{probeText(model)}</span>
                {#if model.probe?.at && !model.probe.state}<span class="muted">· {when(model.probe.at)}</span>{/if}
              </span>
            {/if}
          </span>
          {#if !model.builtin}
          <label class="switch small">
            <input type="checkbox" checked={!!model.enabled} disabled={busy === `enable-${model.id}`} onchange={() => toggle(model)} />
            {t("models.enabled")}
          </label>
          {/if}
          <button class="btn small" type="button" disabled={!model.key || !!model.probe?.state || busy === `probe-${model.id}`} onclick={() => probe(model)}>
            {t("models.probe")}
          </button>
          {#if !model.builtin}
          <button class="btn small" type="button" aria-expanded={open === model.id} onclick={() => edit(model)}>{t("action.edit")}</button>
          <button class="btn small ghost" type="button" disabled={busy === `delete-${model.id}`} onclick={() => remove(model)}>{t("action.remove")}</button>
          {/if}
        </div>
        {#if open === model.id}
          <form
            class="editor stack"
            onsubmit={(e) => {
              e.preventDefault();
              void save(model);
            }}
          >
            <label class="field"><span>{t("models.title")}</span><input type="text" required maxlength={80} bind:value={editTitle} /></label>
            <label class="field"><span>{t("models.upstream")}</span><input type="text" required maxlength={128} autocapitalize="off" spellcheck="false" bind:value={editUpstream} /></label>
            {#if custom(model.provider)}
              <label class="field"><span>{t("models.base_url")}</span><input type="url" required maxlength={200} bind:value={editBaseUrl} /></label>
            {/if}
            <div class="prices">
              {#each PRICES as k (k)}
                <label class="field"><span>{t(`models.${k}`)}</span><input type="number" min="0" step="any" required bind:value={editPrices[k]} /></label>
              {/each}
            </div>
            <label class="field"><span>{t("models.context")}</span><input type="number" min="1000" step="1" required bind:value={editContext} /></label>
            <div class="row">
              <button class="btn small primary" type="submit" disabled={busy === `save-${model.id}`}>{t("action.save")}</button>
              <button class="btn small ghost" type="button" onclick={() => (open = "")}>{t("action.cancel")}</button>
            </div>
          </form>
        {/if}
      </li>
    {:else}
      {#if !listed}<li class="empty">{t("app.loading")}</li>{:else}<li class="empty">{t("models.none")}</li>{/if}
    {/each}
  </ul>
  {:else}
  <form class="panel stack" onsubmit={create}>
    <label class="field">
      <span>{t("models.id")}</span>
      <input type="text" required maxlength={64} pattern={ID_PATTERN} autocapitalize="off" spellcheck="false" bind:value={id} />
    </label>
    <p class="muted small">{t("models.id_hint")}</p>
    <label class="field">
      <span>{t("models.provider")}</span>
      <select bind:value={provider} required>
        {#each providers as p (p.id)}<option value={p.id}>{p.title}</option>{/each}
      </select>
    </label>
    {#if kind?.hint}<p class="muted small">{kind.hint}</p>{/if}
    {#if kind}<p class="muted small">{t(kind.caching ? "models.caching_yes" : "models.caching_no")}</p>{/if}
    {#if custom(provider)}
      <label class="field"><span>{t("models.base_url")}</span><input type="url" required maxlength={200} placeholder="https://" bind:value={baseUrl} /></label>
    {/if}
    <label class="field">
      <span>{t("models.upstream")}</span>
      <input type="text" required maxlength={128} autocapitalize="off" spellcheck="false" bind:value={upstream} />
    </label>
    <p class="muted small">{t("models.upstream_hint")}</p>
    <label class="field"><span>{t("models.title")}</span><input type="text" required maxlength={80} bind:value={title} /></label>
    <div class="prices">
      {#each PRICES as k (k)}
        <label class="field"><span>{t(`models.${k}`)}</span><input type="number" min="0" step="any" required bind:value={prices[k]} /></label>
      {/each}
    </div>
    <label class="field"><span>{t("models.context")}</span><input type="number" min="1000" step="1" required bind:value={context} /></label>
    <p class="muted small">{t("models.prices_hint")}</p>
    <div>
      <button class="btn primary" type="submit" disabled={busy === "create" || !ready}>{t("action.create")}</button>
    </div>
  </form>
  {/if}
{/if}

<style>
  .back { color: var(--muted); text-decoration: none; font-size: 20px; }
  @media (min-width: 900px) { .back { display: none; } } /* «Назад» there */
  .intro { margin: 0 0 8px; }
  .tabs { margin-bottom: 12px; }
  .keys { display: grid; gap: 8px; }
  .models { list-style: none; margin: 0 0 8px; padding: 0; display: grid; gap: 8px; }
  .models .off { opacity: 0.7; }
  .head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .who { flex: 1 1 240px; display: grid; gap: 2px; min-width: 0; }
  .small { font-size: 12px; }
  .warn { color: var(--crit); }
  .good { color: var(--ok, var(--text)); }
  .probe { overflow-wrap: anywhere; }
  .switch { display: inline-flex; gap: 6px; align-items: center; }
  .editor { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--line-soft); }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .prices { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 8px; }
  p.small { margin: 0; }
</style>
