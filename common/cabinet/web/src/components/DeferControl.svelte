<script lang="ts">
  // A card put off until a time: until when, who put it off and why; "Put off until…" on any card not closed
  // and "Wake now" on a deferred one, for whoever moves the card. The moment is the person's own time zone;
  // the backend checks the bounds (10 minutes to 90 days) and says why it refuses.
  import { apiPost } from "../lib/api";
  import { errorText, t } from "../lib/i18n.svelte";
  import { session } from "../lib/session.svelte";
  import { dayTime } from "../lib/time";
  import { toast } from "../lib/toast.svelte";
  import type { Card } from "../lib/types";

  let { card, onchanged }: { card: Card; onchanged: () => void } = $props();

  const defer = $derived(card.pipeline?.defer ?? card.defer ?? null);
  const open = $derived(card.status !== "Done" && card.status !== "Cancelled");
  const may = $derived(open && session.can("cards.transition", card.project));
  const zone = $derived(session.me?.timezone ?? "");
  const who = (by: string) => (by.startsWith("agent:") ? "@" + by.slice(6) : by.startsWith("user:") ? by.slice(5) : by);

  let editing = $state(false);
  let until = $state("");
  let reason = $state("");
  let busy = $state(false);

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (busy || !until) return;
    busy = true;
    try {
      await apiPost(`/api/cards/${encodeURIComponent(card.id)}/defer`, { until, reason: reason.trim() });
      editing = false;
      reason = "";
      onchanged();
    } catch (exc) {
      toast(errorText(exc), "error");
    } finally {
      busy = false;
    }
  }

  async function wake() {
    if (busy) return;
    busy = true;
    try {
      await apiPost(`/api/cards/${encodeURIComponent(card.id)}/undefer`);
      toast(t("defer.woken"));
      onchanged();
    } catch (exc) {
      toast(errorText(exc), "error");
    } finally {
      busy = false;
    }
  }
</script>

{#if defer}
  <p class="lead">{t("defer.lead", { when: dayTime(defer.until, zone) })}</p>
  <p class="hint">{t("defer.by", { who: who(defer.by) })}{defer.reason ? ` — ${defer.reason}` : ""}</p>
{/if}
{#if may}
  {#if editing}
    <form class="defer" onsubmit={save}>
      <label><span class="hint">{t("defer.when")}</span><input type="datetime-local" required bind:value={until} /></label>
      <label><span class="hint">{t("defer.reason")}</span><input type="text" maxlength="500" bind:value={reason} /></label>
      <p class="hint">{t("defer.hint")}</p>
      <span class="buttons">
        <button type="submit" class="btn primary" disabled={busy || !until}>{t("defer.save")}</button>
        <button type="button" class="btn" disabled={busy} onclick={() => (editing = false)}>{t("defer.cancel")}</button>
      </span>
    </form>
  {:else}
    <span class="buttons">
      {#if defer}<button type="button" class="btn primary" disabled={busy} onclick={() => void wake()}>{t("defer.wake")}</button>{/if}
      <button type="button" class="btn" disabled={busy} onclick={() => (editing = true)}>{t("defer.action")}</button>
    </span>
  {/if}
{/if}

<style>
  .lead { margin: 0; font-weight: 600; overflow-wrap: anywhere; }
  .hint { margin: 0; font-size: 13px; color: var(--muted); overflow-wrap: anywhere; }
  .defer { display: flex; flex-direction: column; gap: 8px; }
  .defer label { display: flex; flex-direction: column; gap: 4px; }
  .buttons { display: flex; gap: 8px; flex-wrap: wrap; }
</style>
