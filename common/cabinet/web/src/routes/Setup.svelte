<script lang="ts">
  // Onboarding of a device: install the app, create a passkey, allow notifications; password + TOTP as the
  // fallback. Shown after the first sign-in (must_setup) and reachable later from the settings.
  import { onMount } from "svelte";
  import { apiPost } from "../lib/api";
  import { errorText, t } from "../lib/i18n.svelte";
  import { install, isIos, isStandalone, promptInstall } from "../lib/install.svelte";
  import { deviceName, pushState, subscribePush, type PushState } from "../lib/push";
  import { navigate } from "../lib/router.svelte";
  import { session } from "../lib/session.svelte";
  import { proveStepUp } from "../lib/stepup";
  import { askTotp, confirmed } from "../lib/stepup-dialog.svelte";
  import { isPasskeyAvailable, passkeyErrorKey, registerPasskey } from "../lib/webauthn";

  const standalone = isStandalone();
  const ios = isIos();
  const factors = $derived(session.me?.second_factor ?? { passkey: false, totp: false, password: false });
  const signs = $derived(factors.passkey || factors.totp);

  let push = $state<PushState>("off");
  let busy = $state("");
  let errors = $state<Record<string, string>>({});
  let password = $state("");
  let passwordSaved = $state(false);
  let totpSecret = $state<{ secret: string; uri: string } | null>(null);
  let totpCode = $state("");

  onMount(() => {
    pushState().then((value) => (push = value), () => (push = "unsupported"));
  });

  function failed(step: string, exc: unknown) {
    const key = passkeyErrorKey(exc);
    errors = { ...errors, [step]: key ? t(key) : errorText(exc) };
  }

  async function run(step: string, work: () => Promise<void>) {
    busy = step;
    errors = { ...errors, [step]: "" };
    try {
      await work();
    } catch (exc) {
      failed(step, exc);
    } finally {
      busy = "";
    }
  }

  const addPasskey = () =>
    run("passkey", async () => {
      // a person who already signs confirms first, so the new key is created once
      const proof = signs ? await proveStepUp("passkey.add", askTotp("passkey.add")) : undefined;
      if (proof === null) return;
      const { options } = await apiPost("/api/me/passkeys/options");
      const credential = await registerPasskey(options);
      await apiPost("/api/me/passkeys", { credential, name: deviceName(), stepup: proof ?? undefined });
      await session.load();
    });

  const allowPush = () =>
    run("push", async () => {
      push = await subscribePush(session.me!.push_public_key);
    });

  const savePassword = (event: SubmitEvent) => {
    event.preventDefault();
    return run("password", async () => {
      await confirmed("password.set", (stepup) => apiPost("/api/me/password", { password, stepup }));
      password = "";
      passwordSaved = true;
    });
  };

  const beginTotp = () =>
    run("totp", async () => {
      totpSecret = await confirmed("totp.setup", (stepup) =>
        apiPost("/api/me/totp/setup", stepup ? { stepup } : {}),
      );
    });

  const confirmTotp = (event: SubmitEvent) => {
    event.preventDefault();
    return run("totp", async () => {
      await apiPost("/api/me/totp/confirm", { code: totpCode.trim() });
      totpSecret = null;
      totpCode = "";
      await session.load();
    });
  };

  const grouped = (secret: string) => secret.replace(/(.{4})/g, "$1 ").trim();
</script>

<div class="topbar"><h1>{t("setup.title")}</h1></div>

<ol class="steps">
  <li class="panel">
    <h2>{t("setup.install.title")}</h2>
    {#if standalone || install.installed}
      <p class="ok-text">{t("setup.install.done")}</p>
    {:else if install.prompt}
      <button class="btn" type="button" onclick={promptInstall}>{t("setup.install.button")}</button>
    {:else if ios}
      <p class="muted">{t("setup.install.ios")}</p>
    {:else}
      <p class="muted">{t("setup.install.other")}</p>
    {/if}
  </li>

  <li class="panel">
    <h2>{t("setup.passkey.title")}</h2>
    <p class="muted">{t("onboarding.passkey")}</p>
    {#if factors.passkey}<p class="ok-text">{t("setup.passkey.done")}</p>{/if}
    {#if isPasskeyAvailable()}
      <button class="btn {factors.passkey ? '' : 'primary'}" type="button" disabled={busy !== ""} onclick={addPasskey}>
        {t("setup.passkey.button")}
      </button>
    {:else}
      <p class="note">{t("login.passkey_unavailable")}</p>
    {/if}
    {#if errors.passkey}<p class="error-text" role="alert">{errors.passkey}</p>{/if}
  </li>

  <li class="panel">
    <h2>{t("setup.push.title")}</h2>
    <p class="muted">{t("setup.push.hint")}</p>
    {#if push === "on"}
      <p class="ok-text">{t("setup.push.done")}</p>
    {:else if push === "denied"}
      <p class="error-text">{t("setup.push.denied")}</p>
    {:else if push === "unsupported"}
      <p class="note">{t("setup.push.unsupported")}</p>
    {:else}
      <button class="btn" type="button" disabled={busy !== ""} onclick={allowPush}>{t("setup.push.button")}</button>
    {/if}
    {#if errors.push}<p class="error-text" role="alert">{errors.push}</p>{/if}
  </li>

  <li class="panel">
    <details>
      <summary><h2>{t("setup.fallback.title")}</h2></summary>
      <p class="muted">{t("setup.fallback.hint")}</p>
      <form class="stack" onsubmit={savePassword}>
        <label class="field">
          <span>{t("setup.password.new")}</span>
          <input type="password" autocomplete="new-password" minlength="10" required bind:value={password} />
        </label>
        <button class="btn" type="submit" disabled={busy !== ""}>{t("action.save")}</button>
        {#if passwordSaved}<p class="ok-text">{t("setup.password.done")}</p>{/if}
        {#if errors.password}<p class="error-text" role="alert">{errors.password}</p>{/if}
      </form>

      <div class="stack totp">
        {#if factors.totp}
          <p class="ok-text">{t("setup.totp.done")}</p>
        {:else if totpSecret}
          <div class="field">
            <span>{t("setup.totp.secret")}</span>
            <code class="secret">{grouped(totpSecret.secret)}</code>
            <a href={totpSecret.uri}>{t("setup.totp.open")}</a>
          </div>
          <form class="row" onsubmit={confirmTotp}>
            <input type="text" inputmode="numeric" autocomplete="one-time-code" placeholder={t("sign.totp")} required bind:value={totpCode} />
            <button class="btn" type="submit" disabled={busy !== ""}>{t("action.confirm")}</button>
          </form>
        {:else}
          <button class="btn" type="button" disabled={busy !== ""} onclick={beginTotp}>{t("setup.totp.begin")}</button>
        {/if}
        {#if errors.totp}<p class="error-text" role="alert">{errors.totp}</p>{/if}
      </div>
    </details>
  </li>
</ol>

<div class="row finish">
  <button class="btn primary" type="button" onclick={() => navigate("/")}>
    {session.me?.must_setup ? t("setup.later") : t("setup.finish")}
  </button>
</div>

<style>
  .steps { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--sp-12); }
  .steps li { display: grid; gap: var(--sp-8); }
  h2 { margin: 0; font-size: var(--fs-lead); }
  p { margin: 0; }
  summary { cursor: pointer; list-style: none; }
  summary h2 { display: inline; }
  details[open] summary { margin-bottom: var(--sp-10); }
  details .stack + .stack { margin-top: var(--sp-14); }
  .secret { font-size: var(--fs-lead); letter-spacing: .04em; overflow-wrap: anywhere; }
  .row input { flex: 1; }
  .finish { margin-top: var(--sp-16); justify-content: flex-end; }
</style>
