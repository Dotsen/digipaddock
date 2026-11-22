<script lang="ts">
  // Sign-in: a passkey (the browser offers the person's keys), a one-time code, or a password with TOTP.
  // A successful answer is /api/me; the app then leaves this page (App.svelte).
  import { ApiError, apiGet, apiPost } from "../lib/api";
  import { errorText, t } from "../lib/i18n.svelte";
  import { session, type Me } from "../lib/session.svelte";
  import { assertPasskey, isCancelled, isPasskeyAvailable } from "../lib/webauthn";

  type Mode = "code" | "password";
  const passkeys = isPasskeyAvailable();
  let mode = $state<Mode>("code");
  let user = $state("");
  let code = $state("");
  let password = $state("");
  let totp = $state("");
  let busy = $state(false);
  let error = $state("");

  function failure(exc: unknown): string {
    if (isCancelled(exc)) return t("error.passkey_cancelled");
    if (exc instanceof ApiError && exc.status === 401) return t("login.failed");
    return errorText(exc);
  }

  async function attempt(run: () => Promise<Me>) {
    busy = true;
    error = "";
    try {
      session.signedIn(await run());
    } catch (exc) {
      error = failure(exc);
    } finally {
      busy = false;
    }
  }

  const withPasskey = () =>
    attempt(async () => {
      const { options, login_id } = await apiGet("/api/auth/passkey/options");
      const credential = await assertPasskey(options);
      return apiPost<Me>("/api/auth/passkey", { login_id, credential });
    });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    if (mode === "code") {
      void attempt(() => apiPost<Me>("/api/auth/code", { user: user.trim(), code: code.trim() }));
    } else {
      void attempt(() =>
        apiPost<Me>("/api/auth/password", { user: user.trim(), password, totp: totp.trim() }),
      );
    }
  }
</script>

<section class="login">
  <h1>{t("login.title")}</h1>

  {#if passkeys}
    <button class="btn primary wide" type="button" disabled={busy} onclick={withPasskey}>{t("login.passkey")}</button>
  {:else}
    <p class="note">{t("login.passkey_unavailable")}</p>
  {/if}

  <div class="segmented" role="tablist">
    <button type="button" role="tab" class:on={mode === "code"} aria-selected={mode === "code"} onclick={() => (mode = "code")}>
      {t("login.by_code")}
    </button>
    <button
      type="button"
      role="tab"
      class:on={mode === "password"}
      aria-selected={mode === "password"}
      onclick={() => (mode = "password")}>{t("login.by_password")}</button
    >
  </div>

  <form class="stack" onsubmit={submit}>
    <label class="field">
      <span>{t("login.user")}</span>
      <input type="text" name="user" autocomplete="username" autocapitalize="none" required bind:value={user} />
    </label>
    {#if mode === "code"}
      <label class="field">
        <span>{t("login.code")}</span>
        <input type="text" name="code" autocomplete="one-time-code" autocapitalize="none" required bind:value={code} />
      </label>
      <p class="muted hint">{t("login.code_hint")}</p>
    {:else}
      <label class="field">
        <span>{t("login.password")}</span>
        <input type="password" name="password" autocomplete="current-password" required bind:value={password} />
      </label>
      <label class="field">
        <span>{t("login.totp")}</span>
        <input type="text" name="totp" inputmode="numeric" autocomplete="one-time-code" required bind:value={totp} />
      </label>
    {/if}
    {#if error}<p class="error-text" role="alert">{error}</p>{/if}
    <button class="btn wide" type="submit" disabled={busy}>{t("login.submit")}</button>
  </form>
</section>

<style>
  .login { max-width: 380px; margin: 0 auto; padding: 48px 0; display: grid; gap: 16px; }
  h1 { margin: 0 0 8px; font-size: 24px; }
  .hint { margin: 0; font-size: 13px; }
</style>
