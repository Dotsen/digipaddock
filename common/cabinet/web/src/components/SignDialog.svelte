<script lang="ts">
  // Signing one decision or a batch of one type: a challenge over them as shown, a passkey assertion (or a
  // TOTP code when the passkey is not at hand), the term of a plan's grant. A 409 means a card changed:
  // the page re-reads before anyone signs again. A pull request is signed with its mode and a passkey
  // only: kd-review checks a passkey signature itself, a TOTP code it could not check.
  import { untrack } from "svelte";
  import { ApiError } from "../lib/api";
  import { NoPasskey, approveDecisions, withPasskey, withTotp } from "../lib/decide";
  import { errorText, t, type Key } from "../lib/i18n.svelte";
  import { session } from "../lib/session.svelte";
  import { trustTitle } from "../lib/trust";
  import type { DecisionItem, PrMode } from "../lib/types";
  import { isCancelled } from "../lib/webauthn";

  let {
    items,
    mode,
    onclose,
    ondone,
    onreread,
  }: {
    items: DecisionItem[];
    /** What a signature on a pull request asks for. */
    mode?: PrMode;
    onclose: () => void;
    ondone: () => void;
    onreread: () => void;
  } = $props();

  const type = $derived(items[0]?.decision.type ?? "");
  const ttlOptions = $derived(items[0]?.view.ttl_options ?? []);
  // a server plan or a critical command is signed with a passkey only — the target verifies that
  // signature, a code it could not (the server refuses TOTP for them anyway; the page does not offer it).
  // every decision of a batch counts, not only the first — a factory plan first must not offer the code
  const needsPasskey = (item: DecisionItem) =>
    item.decision.type === "admin_critical" || (item.decision.type === "admin_plan" && item.view.policy !== "factory");
  const passkeyOnly = $derived(!!mode || items.some(needsPasskey));
  let ttl = $state(72);
  let phase = $state<"ready" | "working" | "totp" | "conflict">("ready");
  let error = $state("");
  let conflict = $state("");
  let code = $state("");
  let totpAvailable = $state(untrack(() => !passkeyOnly));

  const term = () => (ttlOptions.length ? ttl : undefined);

  async function run(prove: Parameters<typeof approveDecisions>[1], fallback: "ready" | "totp") {
    phase = "working";
    error = "";
    try {
      await approveDecisions(items, prove, term(), mode);
      ondone();
    } catch (exc) {
      if (exc instanceof ApiError && exc.status === 409) {
        conflict = t(
          exc.kind === "stale" ? "error.stale" : exc.kind === "approved_in_forge" ? "error.approved_in_forge" : "error.conflict",
        );
        phase = "conflict";
        return;
      }
      if (exc instanceof NoPasskey || isCancelled(exc)) {
        if (exc instanceof NoPasskey) totpAvailable = exc.totp && !passkeyOnly;
        if (exc instanceof NoPasskey) error = totpAvailable ? "" : t("error.setup_required");
        else error = t("error.passkey_cancelled");
        phase = totpAvailable ? "totp" : "ready";
        return;
      }
      error =
        mode && exc instanceof ApiError && exc.kind === "setup_required"
          ? t("decision.pr.setup_required", { user: session.me?.id ?? "" })
          : errorText(exc);
      phase = fallback;
    }
  }

  const sign = () => run(withPasskey, "ready");

  function submitTotp(event: SubmitEvent) {
    event.preventDefault();
    const value = code.trim();
    code = "";
    void run(withTotp(value), "totp");
  }

  const titleOf = (item: DecisionItem) =>
    item.decision.type === "monitor_trust" // no card — what becomes trusted
      ? trustTitle({ ...item.decision.payload, incident: item.view.incident ?? item.decision.payload.incident })
      : item.view.intent || item.view.title || item.view.card?.title || item.decision.card;
  const shown = (value: unknown) => {
    const text = String(value ?? "");
    return text.length > 120 ? `${text.slice(0, 119)}…` : text;
  };
</script>

<div class="veil" role="presentation" onclick={(e) => e.target === e.currentTarget && phase !== "working" && onclose()}>
  <div class="sheet" role="dialog" aria-modal="true" aria-labelledby="sign-title">
    <h2 id="sign-title">{t("sign.title")}</h2>
    <div class="what">
      {#if items.length > 1}
        <b>{t("batch.title", { count: items.length })}</b>
        <span>{t("batch.hint")}</span>
      {/if}
      <span class="chip">{t(`decision.type.${type}` as Key)}</span>
      {#if type === "monitor_trust" && items.length > 1}
        <!-- a bundle of accepts — how much, where, and each object's value as it will be trusted -->
        <span>{t("inbox.bundle", { n: items.length, servers: new Set(items.map((i) => i.decision.payload?.server)).size })}</span>
      {/if}
      {#each items as item (item.decision.id)}
        <b>{item.decision.card ? `${item.decision.card} · ` : ""}{titleOf(item)}</b>
        {#if type === "monitor_trust" && (item.decision.payload?.kind === "accept" || item.decision.payload?.kind === "revoke")}
          <span class="muted trust-value">{item.decision.payload.server}: {shown(item.decision.payload.value)}</span>
        {/if}
      {/each}
      {#if mode}
        <b>{t(`decision.pr.mode.${mode}` as Key)}</b>
        <span>{t("decision.pr.passkey_only")}</span>
      {:else if passkeyOnly}
        <span>{t("sign.passkey_only")}</span>
      {/if}
    </div>

    {#if phase === "conflict"}
      <p class="error-text" role="alert">{conflict}</p>
      <button class="btn primary" type="button" onclick={onreread}>{t("sign.reread")}</button>
    {:else}
      {#if ttlOptions.length}
        <label class="field">
          <span>{t("decision.ttl")}</span>
          <select bind:value={ttl}>
            {#each ttlOptions as hours (hours)}
              <option value={hours}>{t("time.hours", { n: hours })}</option>
            {/each}
          </select>
        </label>
      {/if}

      {#if phase === "totp"}
        <form class="row" onsubmit={submitTotp}>
          <input
            type="text"
            inputmode="numeric"
            autocomplete="one-time-code"
            placeholder={t("sign.totp")}
            aria-label={t("sign.totp")}
            required
            bind:value={code}
          />
          <button class="btn primary" type="submit">{t("action.confirm")}</button>
        </form>
      {:else}
        <p class="muted prompt">{t("sign.prompt")}</p>
      {/if}

      {#if error}<p class="error-text" role="alert">{error}</p>{/if}

      <div class="actions">
        <button class="btn" type="button" disabled={phase === "working"} onclick={onclose}>{t("action.cancel")}</button>
        {#if phase !== "totp"}
          <button class="btn primary" type="button" disabled={phase === "working"} onclick={sign}>{t("action.sign")}</button>
        {/if}
      </div>
      {#if phase === "ready" && totpAvailable}
        <button class="link" type="button" onclick={() => ((phase = "totp"), (error = ""))}>{t("sign.use_totp")}</button>
      {/if}
    {/if}
  </div>
</div>

<style>
  .what { background: var(--bg); border-radius: var(--r-md); padding: var(--sp-10) var(--sp-12); font-size: var(--fs-md); color: var(--muted); display: grid; gap: var(--sp-4); justify-items: start; min-width: 0; overflow-wrap: anywhere; }
  .what b { color: var(--text); font-weight: 500; }
  .prompt { margin: 0; font-size: var(--fs-ui); }
  .row input { flex: 1; }
  .link { background: none; border: 0; color: var(--accent); font-size: var(--fs-md); justify-self: center; }
</style>
