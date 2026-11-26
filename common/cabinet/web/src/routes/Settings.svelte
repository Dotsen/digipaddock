<script lang="ts">
  // Settings of the person: profile (name, language at once, time zone), passkeys, signed-in devices,
  // notifications (rules, quiet hours, this device's push), and — with `settings` — token spend. People and
  // roles have their own page, projects too. Removing a key and a sign-in code for a new device
  // go through a step-up (StepUp).
  import { onMount } from "svelte";
  import LoginCode from "../components/LoginCode.svelte";
  import PrefsTable, { type Mode } from "../components/PrefsTable.svelte";
  import UsageChart from "../components/UsageChart.svelte";
  import { ApiError, apiDelete, apiGet, apiPatch, apiPost, apiPut } from "../lib/api";
  import { errorText, t } from "../lib/i18n.svelte";
  import { deviceName, pushDetails, subscribePush, unsubscribePush, type PushLast, type PushState } from "../lib/push";
  import { lastLine } from "../lib/pushview";
  import { session, type Me } from "../lib/session.svelte";
  import { proveStepUp } from "../lib/stepup";
  import { askTotp, confirmed } from "../lib/stepup-dialog.svelte";
  import { age, when } from "../lib/time";
  import { toast } from "../lib/toast.svelte";
  import type { Passkey, UsageRow, WebSession } from "../lib/types";
  import { passkeyErrorKey, registerPasskey } from "../lib/webauthn";

  interface Prefs {
    kinds: Record<string, Mode>;
    quiet: string;
  }

  const me = $derived(session.me!);
  const signs = $derived(me.second_factor.passkey || me.second_factor.totp);
  const people = $derived(session.can("users.manage") || session.can("settings"));
  const spend = $derived(session.can("settings"));
  const zones: string[] = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];

  let name = $state("");
  let timezone = $state("");
  let keys = $state<Passkey[] | null>(null);
  let devices = $state<WebSession[] | null>(null);
  let prefs = $state<Prefs | null>(null);
  let quietOn = $state(false);
  let quietFrom = $state("23:00");
  let quietTo = $state("08:00");
  let push = $state<PushState>("off");
  let pushLast = $state<PushLast | null>(null);
  const lastInfo = $derived(lastLine(pushLast));
  let days = $state(7);
  let usage = $state<UsageRow[] | null>(null);
  let busy = $state("");
  let armed = $state(""); // a destructive button pressed once waits for the second press

  const failed = (exc: unknown) => {
    const key = passkeyErrorKey(exc);
    toast(key ? t(key) : errorText(exc), "error");
  };

  async function run(step: string, work: () => Promise<unknown>) {
    busy = step;
    armed = "";
    try {
      await work();
    } catch (exc) {
      failed(exc);
    } finally {
      busy = "";
    }
  }

  function arm(step: string, work: () => Promise<unknown>) {
    if (armed !== step) {
      armed = step;
      return;
    }
    void run(step, work);
  }

  function showPrefs(value: Prefs) {
    prefs = value;
    const [from, to] = value.quiet ? value.quiet.split("-") : ["", ""];
    quietOn = !!value.quiet;
    if (from) quietFrom = from;
    if (to) quietTo = to;
  }

  onMount(() => {
    name = me.name;
    timezone = me.timezone;
    apiGet<Passkey[]>("/api/me/passkeys").then((v) => (keys = v), failed);
    apiGet<WebSession[]>("/api/me/sessions").then((v) => (devices = v), failed);
    apiGet<Prefs>("/api/me/notifications").then(showPrefs, failed);
    readPush();
  });

  // this device's state and its last delivery as the gateway answered (accepted is not shown-for-sure)
  function readPush() {
    pushDetails().then(
      (found) => {
        push = found.state;
        pushLast = found.last;
      },
      () => (push = "unsupported"),
    );
  }

  $effect(() => {
    if (!spend) return;
    const n = days;
    let current = true;
    usage = null;
    apiGet<UsageRow[]>(`/api/usage?days=${n}`).then((v) => {
      if (current) usage = v;
    }, (exc) => { if (current) failed(exc); });
    return () => { current = false; };
  });

  const saveProfile = (event: SubmitEvent) => {
    event.preventDefault();
    return run("profile", async () => {
      session.signedIn(await apiPatch<Me>("/api/me", { name: name.trim(), timezone: timezone.trim() }));
      toast(t("settings.saved"), "info");
    });
  };

  const setLanguage = (language: string) =>
    run("language", async () => session.signedIn(await apiPatch<Me>("/api/me", { language })));

  const addKey = () =>
    run("key", async () => {
      // a person who already signs confirms first, so the new key is created once
      const proof = signs ? await proveStepUp("passkey.add", askTotp("passkey.add")) : null;
      if (signs && !proof) return;
      const { options } = await apiPost("/api/me/passkeys/options");
      const credential = await registerPasskey(options);
      const added = await apiPost<{ passkeys: Passkey[] }>("/api/me/passkeys", {
        credential,
        name: deviceName(),
        stepup: proof ?? undefined,
      });
      keys = added.passkeys;
      await session.load();
    });

  const removeKey = (key: Passkey) =>
    arm(`key-${key.id}`, async () => {
      try {
        keys = await confirmed("passkey.remove", (stepup) =>
          apiDelete<Passkey[]>(`/api/me/passkeys/${key.id}`, stepup ? { stepup } : undefined),
        );
      } catch (exc) {
        // the last key stays until a password with TOTP can stand in for it
        if (!(exc instanceof ApiError && exc.kind === "conflict")) throw exc;
        toast(t("settings.keys.last"), "error");
        return;
      }
      await session.load();
    });

  const closeDevice = (device: WebSession) =>
    run(`device-${device.id}`, async () => {
      await apiDelete(`/api/me/sessions/${encodeURIComponent(device.id)}`);
      devices = (devices ?? []).filter((d) => d.id !== device.id);
    });

  // a code to sign in on a new device — a new sign-in, so a passkey (or TOTP) confirms it
  let deviceCode = $state<{ code: string; expires: string } | null>(null);
  const newDeviceCode = () =>
    run("device-code", async () => {
      deviceCode = await confirmed("code.self", (stepup) =>
        apiPost<{ code: string; expires: string }>("/api/me/code", stepup ? { stepup } : {}),
      );
    });

  const logoutEverywhere = () => arm("everywhere", () => session.logout({ everywhere: true }));

  async function setMode(kind: string, mode: Mode) {
    if (!prefs) return;
    const before = prefs.kinds[kind];
    prefs.kinds[kind] = mode;
    busy = "prefs";
    try {
      showPrefs(await apiPut<Prefs>("/api/me/notifications", { kinds: { [kind]: mode } }));
    } catch (exc) {
      if (prefs) prefs.kinds[kind] = before;
      failed(exc);
    } finally {
      busy = "";
    }
  }

  const saveQuiet = (event: SubmitEvent) => {
    event.preventDefault();
    return run("quiet", async () => {
      const quiet = quietOn ? `${quietFrom}-${quietTo}` : "";
      showPrefs(await apiPut<Prefs>("/api/me/notifications", { kinds: {}, quiet }));
      toast(t("settings.saved"), "info");
    });
  };

  const pushOn = () => run("push", async () => (push = await subscribePush(me.push_public_key)));
  const pushOff = () => run("push", async () => (push = await unsubscribePush()));
  const pushTest = () =>
    run("push-test", async () => {
      await apiPost("/api/push/test");
      toast(t("settings.push.sent"), "info");
      setTimeout(readPush, 5000); // the worker sends within seconds: show what the gateway answered
    });
</script>

<div class="topbar"><h1>{t("nav.settings")}</h1></div>

<div class="stack sections">
  <section class="panel stack">
    <h2>{t("settings.profile")}</h2>
    <form class="stack" onsubmit={saveProfile}>
      <label class="field"><span>{t("settings.name")}</span><input type="text" required maxlength="80" bind:value={name} /></label>
      <label class="field">
        <span>{t("settings.timezone")}</span>
        <input type="text" list="zones" required bind:value={timezone} />
        <datalist id="zones">{#each zones as zone (zone)}<option value={zone}></option>{/each}</datalist>
      </label>
      <div><button class="btn primary" type="submit" disabled={busy === "profile"}>{t("action.save")}</button></div>
    </form>
    <label class="field">
      <span>{t("settings.language")}</span>
      <select value={me.language} disabled={busy === "language"} onchange={(e) => setLanguage(e.currentTarget.value)}>
        <option value="ru">Русский</option>
        <option value="en">English</option>
      </select>
    </label>
  </section>

  <section class="panel stack">
    <h2>{t("settings.passkeys")}</h2>
    {#if keys === null}
      <p class="muted">{t("app.loading")}</p>
    {:else if keys.length === 0}
      <p class="muted">{t("settings.keys.none")}</p>
    {:else}
      <ul class="rows">
        {#each keys as key (key.id)}
          <li>
            <span class="grow">
              <b>{key.name || t("settings.keys.unnamed")}</b>
              <span class="muted small">
                {t("settings.keys.added", { when: when(key.created) })}{#if key.last_used}{" · "}{t("settings.keys.used", {
                    age: age(key.last_used),
                  })}{/if}
              </span>
            </span>
            {#if key.signing_enabled}<span class="chip good">{t("settings.keys.signs")}</span>{/if}
            <button
              class="btn small"
              class:danger={armed === `key-${key.id}`}
              type="button"
              disabled={busy === `key-${key.id}`}
              onclick={() => removeKey(key)}
            >
              {armed === `key-${key.id}` ? t("action.sure") : t("action.remove")}
            </button>
          </li>
        {/each}
      </ul>
    {/if}
    <div class="row">
      <button class="btn" type="button" disabled={busy === "key"} onclick={addKey}>{t("settings.keys.add")}</button>
      <a class="btn ghost" href="/setup">{t("settings.password")}</a>
    </div>
  </section>

  <section class="panel stack">
    <h2>{t("settings.devices")}</h2>
    {#if devices === null}
      <p class="muted">{t("app.loading")}</p>
    {:else}
      <ul class="rows">
        {#each devices as device (device.id)}
          <li>
            <span class="grow">
              <b>{device.device ? (device.device.startsWith("Mozilla/") ? deviceName(device.device) : device.device) : t("settings.devices.unknown")}</b>
              <span class="muted small">{t("settings.devices.seen", { age: age(device.last_seen) })}</span>
            </span>
            {#if device.current}
              <span class="chip acc">{t("settings.devices.this")}</span>
            {:else}
              <button class="btn small" type="button" disabled={busy === `device-${device.id}`} onclick={() => closeDevice(device)}>
                {t("settings.devices.close")}
              </button>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
    {#if deviceCode}
      <LoginCode
        title={t("settings.device_code.title")}
        code={deviceCode.code}
        expires={deviceCode.expires}
        hint={t("settings.device_code.hint", { user: me.id })}
        onclose={() => (deviceCode = null)}
      />
    {/if}
    <div class="row">
      {#if signs}
        <button class="btn" type="button" disabled={busy === "device-code"} onclick={newDeviceCode}>{t("settings.device_code")}</button>
      {:else}
        <span class="muted small">{t("settings.device_code.setup")}</span>
      {/if}
      <button class="btn" class:danger={armed === "everywhere"} type="button" onclick={logoutEverywhere}>
        {armed === "everywhere" ? t("action.sure") : t("settings.logout_all")}
      </button>
    </div>
  </section>

  <section class="panel stack">
    <h2>{t("settings.push")}</h2>
    <div class="row">
      {#if push === "on"}
        <span class="chip good">{t("setup.push.done")}</span>
        <button class="btn small" type="button" disabled={busy === "push-test"} onclick={pushTest}>{t("settings.push.test")}</button>
        <button class="btn small" type="button" disabled={busy === "push"} onclick={pushOff}>{t("settings.push.off")}</button>
      {:else if push === "off" || push === "lost"}
        {#if push === "lost"}<span class="muted">{t("push.banner.lost", { name: "" })}</span>{/if}
        <button class="btn" type="button" disabled={busy === "push"} onclick={pushOn}>{t("setup.push.button")}</button>
      {:else if push === "denied"}
        <span class="muted">{t("push.banner.denied", { name: "" })}</span>
      {:else if push === "install"}
        <span class="muted">{t("push.banner.install", { name: "" })}</span>
      {:else}
        <span class="muted">{t("setup.push.unsupported")}</span>
      {/if}
    </div>
    {#if push === "on"}
      <p class="muted small">
        {t(lastInfo.key, { code: lastInfo.code, at: lastInfo.at ? when(lastInfo.at) : "" })}
        <button class="btn small ghost" type="button" onclick={readPush}>{t("app.retry")}</button>
      </p>
    {/if}
    {#if prefs}
      <PrefsTable kinds={prefs.kinds} disabled={busy === "prefs"} onchange={setMode} />
      <form class="quiet" onsubmit={saveQuiet}>
        <label class="check"><input type="checkbox" bind:checked={quietOn} /> {t("settings.quiet")}</label>
        <input type="time" aria-label={t("settings.quiet.from")} disabled={!quietOn} bind:value={quietFrom} />
        <span>—</span>
        <input type="time" aria-label={t("settings.quiet.to")} disabled={!quietOn} bind:value={quietTo} />
        <button class="btn small" type="submit" disabled={busy === "quiet"}>{t("action.save")}</button>
      </form>
      <p class="muted small">{t("settings.quiet.hint")}</p>
    {:else}
      <p class="muted">{t("app.loading")}</p>
    {/if}
  </section>

  {#if people}
    <section class="panel row between">
      <h2>{t("settings.users")}</h2>
      <a class="btn" href="/settings/users">{t("action.open")}</a>
    </section>
  {/if}

  {#if spend}
    {#if session.me?.memory}
      <section class="panel row between">
        <div><h2>Hindsight</h2><p class="muted small">{t('hindsight.settings_hint')}</p></div>
        <a class="btn" href="/settings/hindsight">{t("action.open")}</a>
      </section>
    {/if}

    <section class="panel row between">
      <h2>{t("settings.projects")}</h2>
      <a class="btn" href="/settings/projects">{t("action.open")}</a>
    </section>

    <section class="panel row between">
      <div><h2>{t("settings.models")}</h2><p class="muted small">{t("models.settings_hint")}</p></div>
      <a class="btn" href="/settings/models">{t("action.open")}</a>
    </section>

    <section class="panel stack">
      <div class="row between">
        <h2>{t("settings.usage")}</h2>
        <select aria-label={t("settings.usage.days")} bind:value={days}>
          <option value={7}>{t("settings.usage.week")}</option>
          <option value={30}>{t("settings.usage.month")}</option>
        </select>
      </div>
      {#if usage === null}
        <p class="muted">{t("app.loading")}</p>
      {:else}
        <UsageChart rows={usage} />
      {/if}
    </section>
  {/if}

  <section class="panel row between">
    <h2>{t("setup.title")}</h2>
    <a class="btn" href="/setup">{t("action.open")}</a>
  </section>

  <!-- the phone has no side menu: signing out lives here too -->
  <section class="panel row between">
    <span class="muted">{me.name}</span>
    <button class="btn" type="button" onclick={() => session.logout()}>{t("action.logout")}</button>
  </section>
</div>

<style>
  .sections { gap: var(--sp-14); padding-bottom: var(--sp-20); }
  h2 { margin: 0; font-size: var(--fs-lead); }
  .rows { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--sp-6); }
  .rows li { display: flex; gap: var(--sp-8); align-items: center; flex-wrap: wrap; padding: var(--sp-6) 0; border-bottom: 1px solid var(--line-soft); }
  .grow { flex: 1 1 200px; display: grid; gap: var(--sp-2); min-width: 0; }
  .small { font-size: var(--fs-sm); }
  .row { display: flex; gap: var(--sp-8); align-items: center; flex-wrap: wrap; }
  .between { justify-content: space-between; }
  .between select { width: auto; }
  .quiet { display: flex; gap: var(--sp-8); align-items: center; flex-wrap: wrap; }
  .check { display: flex; gap: var(--sp-6); align-items: center; }
  .btn.danger { color: var(--crit); border-color: var(--crit); }
</style>
