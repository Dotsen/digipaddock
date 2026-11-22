<script lang="ts">
  import { ApiError, apiPost } from "../lib/api";
  import { errorText, t } from "../lib/i18n.svelte";
  import { changeProviderKey } from "../lib/model-key";
  import { session } from "../lib/session.svelte";
  import { toast } from "../lib/toast.svelte";
  import type { LlmProvider } from "../lib/types";
  import { when } from "../lib/time";

  let { provider, changed }: { provider: LlmProvider; changed: () => Promise<void> } = $props();
  let key = $state("");
  let busy = $state(false);

  async function save(action: "put" | "delete") {
    if (action === "delete" && !confirm(t("models.key_delete_confirm", { provider: provider.title }))) return;
    const value = key;
    key = ""; // no draft, storage, clipboard or value prefilled from the host
    busy = true;
    try {
      const result = await changeProviderKey(provider.id, action, value);
      toast(t(result.audit ? (action === "put" ? "models.key_saved" : "models.key_deleted") : "models.key_audit_failed"),
        result.audit ? "info" : "error");
      if (result.restart === false) toast(t("models.subscription_restart_failed"), "error");
      await changed();
    } catch (exc) {
      // root has enrolled no signing passkey yet — say who does what, not «add a passkey or TOTP»
      const unenrolled = exc instanceof ApiError && exc.kind === "setup_required";
      toast(unenrolled ? t("models.key_setup_required", { user: session.me?.id ?? "" }) : errorText(exc), "error");
    } finally {
      key = "";
      busy = false;
    }
  }

  async function probe() {
    busy = true;
    try {
      await apiPost("/api/model-credentials/subscription/probe", {});
      await changed();
    } catch (exc) {
      toast(errorText(exc), "error");
    } finally {
      busy = false;
    }
  }
</script>

<form class="panel stack" onsubmit={(event) => { event.preventDefault(); void save("put"); }}>
  <div class="row"><b>{provider.title}</b><span class="small muted">{t(provider.key ? "models.key_present" : "models.key_missing")}</span></div>
  {#if provider.key_type}<p class="small muted">{t(`models.key_type.${provider.key_type}`)}</p>{/if}
  {#if provider.replace_only}
    <p class="small muted">{t("models.subscription_help")}</p>
    {#if provider.key && provider.expires}
      <p class="small" class:warn={!!provider.warning}>{t("models.subscription_age", { days: provider.age_days ?? 0, expires: when(provider.expires) })}</p>
      {#if provider.warning}<p class="small warn">{t(provider.warning === "expired" ? "models.subscription_expired" : "models.subscription_warning", { days: provider.days_left ?? 0 })}</p>{/if}
    {/if}
    {#if provider.probe?.state}<p class="small muted">{t(provider.probe.state === "running" ? "models.probe_running" : "models.probe_requested")}</p>
    {:else if provider.probe?.at}<p class="small" class:warn={provider.probe.ok === false}>{t(provider.probe.ok ? "models.subscription_probe_ok" : "models.subscription_probe_failed", { detail: provider.probe.detail ?? "" })} · {when(provider.probe.at)}</p>{/if}
  {/if}
  <label class="field">
    <span>{t("models.key_label", { provider: provider.title })}</span>
    <input type="password" bind:value={key} autocomplete="off" autocapitalize="off" spellcheck="false"
      maxlength={4096} required disabled={busy} />
  </label>
  <div class="row">
    <button class="btn small primary" type="submit" disabled={busy || !key}>{t("models.key_save")}</button>
    {#if provider.replace_only}<button class="btn small" type="button" disabled={busy || !provider.key || !!provider.probe?.state} onclick={probe}>{t("models.probe")}</button>
    {:else if provider.key}<button class="btn small ghost" type="button" disabled={busy} onclick={() => save("delete")}>{t("models.key_delete")}</button>{/if}
  </div>
</form>

<style>
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .small { font-size: 12px; }
</style>
