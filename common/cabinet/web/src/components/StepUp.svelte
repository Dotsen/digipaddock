<script lang="ts">
  // The step-up dialog for a TOTP code (sensitive settings: keys, sessions, people, roles). The app mounts
  // it once; `confirmed()` in lib/stepup-dialog opens it only when the person confirms without a passkey.
  import { hasKey, t, type Key } from "../lib/i18n.svelte";
  import { stepUpPrompt } from "../lib/stepup-dialog.svelte";

  let code = $state("");
  const what = $derived(
    hasKey(`stepup.purpose.${stepUpPrompt.purpose}`) ? t(`stepup.purpose.${stepUpPrompt.purpose}` as Key) : "",
  );

  function submit(event: SubmitEvent) {
    event.preventDefault();
    const value = code.trim();
    code = "";
    if (value) stepUpPrompt.answer(value);
  }

  function cancel() {
    code = "";
    stepUpPrompt.answer(null);
  }
</script>

{#if stepUpPrompt.open}
  <div class="veil" role="presentation" onclick={(e) => e.target === e.currentTarget && cancel()}>
    <div class="sheet" role="dialog" aria-modal="true" aria-labelledby="stepup-title">
      <h2 id="stepup-title">{t("stepup.title")}</h2>
      {#if what}<p class="muted what">{what}</p>{/if}
      <form class="stack" onsubmit={submit}>
        <label class="field">
          <span>{t("stepup.code")}</span>
          <!-- svelte-ignore a11y_autofocus -->
          <input type="text" inputmode="numeric" autocomplete="one-time-code" required autofocus bind:value={code} />
        </label>
        <div class="actions">
          <button class="btn" type="button" onclick={cancel}>{t("action.cancel")}</button>
          <button class="btn primary" type="submit">{t("action.confirm")}</button>
        </div>
      </form>
    </div>
  </div>
{/if}

<style>
  .what { margin: 0; font-size: 14px; }
</style>
