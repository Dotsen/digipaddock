<script lang="ts">
  // The status chip of a person who may move the card: a manual change past the card's actions. Picking
  // a status asks why first: one of the target status's ready reasons or «other» with the person's own text
  // ; a ready reason may carry an explanation, a duplicate the number of its original. The thread says it
  // was done by hand.
  import { t, type Key } from "../lib/i18n.svelte";
  import { OVERRIDE_DUPLICATE, OVERRIDE_OTHER, overrideReasons, type OverrideReason } from "../lib/overrideReasons";
  import { STATUSES } from "../lib/types";
  import ChipMenu from "./ChipMenu.svelte";
  import { statusKey, statusTone } from "./StatusPill.svelte";

  let {
    status,
    busy = false,
    onoverride,
  }: {
    status: string;
    busy?: boolean;
    onoverride: (status: string, reason: OverrideReason) => void;
  } = $props();

  let target = $state<string | null>(null);
  let code = $state("");
  let text = $state("");
  let canonical = $state("");
  let trigger = $state<HTMLSpanElement>();
  const options = $derived(STATUSES.map((s) => ({ value: s, text: t(statusKey(s)), tone: statusTone(s) })));
  const reasons = $derived(target ? overrideReasons(target) : []);
  const label = $derived(t(code === OVERRIDE_OTHER ? "flow.override.other" : "flow.override.note"));
  const ready = $derived(!!code && (code !== OVERRIDE_OTHER || text.trim().length > 0) && !busy);

  function open(next: string) {
    code = text = canonical = "";
    target = next;
  }

  function openDialog(dialog: HTMLDialogElement) {
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialog.showModal();
    dialog.querySelector<HTMLInputElement>("input[type=radio]")?.focus();
    return { destroy() {
      dialog.close();
      document.body.style.overflow = overflow;
      trigger?.querySelector("button")?.focus();
    } };
  }

  function send() {
    const next = target;
    if (!next || !ready) return;
    const reason: OverrideReason = { code, text: text.trim() };
    if (code === OVERRIDE_DUPLICATE && canonical.trim()) reason.canonical = canonical.trim();
    target = null;
    onoverride(next, reason);
  }

  function onKey(event: KeyboardEvent) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      send();
    }
  }
</script>

<span bind:this={trigger}><ChipMenu label={t("action.override")} value={status} tone={statusTone(status)} {options} disabled={busy} onchange={open} /></span>
{#if target}
  <dialog
    class="sheet status-dialog"
    aria-label={t("action.override")}
    use:openDialog
    oncancel={() => (target = null)}
    onclick={(event) => { if (event.target === event.currentTarget) target = null; }}
    onkeydown={onKey}
  >
    <div class="dialog-content">
      <h2>{t("action.override")}</h2>
      <div class="transition"><span>{t(statusKey(status))}</span><span aria-hidden="true">→</span><b>{t(statusKey(target))}</b></div>
      <p class="hint">{t("flow.override.hint")}</p>
      <div class="reasons" role="radiogroup" aria-label={t("flow.override.reason")}>
        {#each reasons as reason (reason)}
          <label class="reason" class:on={code === reason}>
            <input type="radio" name="override-reason" value={reason} bind:group={code} />
            {t(`flow.override.code.${reason}` as Key)}
          </label>
        {/each}
      </div>
      {#if code === OVERRIDE_DUPLICATE}
        <input class="field" type="text" aria-label={t("flow.override.canonical")} placeholder={t("flow.override.canonical")} bind:value={canonical} maxlength="40" autocomplete="off" />
      {/if}
      <textarea class="field" rows="3" aria-label={label} placeholder={label} bind:value={text} maxlength="500"></textarea>
      <div class="row">
        <button type="button" class="btn primary small" disabled={!ready} onclick={send}>{t("action.send")}</button>
        <button type="button" class="btn ghost small" onclick={() => (target = null)}>{t("action.cancel")}</button>
      </div>
    </div>
  </dialog>
{/if}

<style>
  .status-dialog { margin: auto auto 0; max-height: calc(100dvh - 16px); color: var(--text); padding: 0; }
  .status-dialog::backdrop { background: var(--scrim); }
  .dialog-content { min-width: 0; padding: var(--sp-16) var(--sp-16) calc(var(--sp-16) + env(safe-area-inset-bottom, 0px)); display: grid; gap: var(--sp-12); }
  .transition { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-8); font-size: var(--fs-ui); color: var(--muted); }
  .transition b { color: var(--text); }
  .hint { margin: 0; font-size: var(--fs-md); color: var(--muted); }
  .reasons { display: flex; flex-wrap: wrap; gap: var(--sp-8); }
  .reason { position: relative; display: inline-flex; align-items: center; min-height: 36px; padding: var(--sp-6) var(--sp-12); border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); cursor: pointer; font-size: var(--fs-ui); }
  .reason.on { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
  .reason:focus-within { outline: 2px solid var(--accent); outline-offset: 2px; }
  .reason input { position: absolute; opacity: 0; width: 1px; height: 1px; margin: 0; }
  .field { width: 100%; box-sizing: border-box; font: inherit; padding: var(--sp-8) var(--sp-10); border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); color: var(--text); }
  textarea.field { resize: vertical; }
  .row { display: flex; gap: var(--sp-8); }
  @media (min-width: 900px) { .status-dialog { margin: auto; max-height: calc(100dvh - 32px); } }
</style>
