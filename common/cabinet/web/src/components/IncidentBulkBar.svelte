<script lang="ts">
  // what to do with the picked incidents at once — acknowledge, mute, unmute each through its own route,
  // or accept the acceptable ones into the baseline as one bundle: the signing dialog shows what each decision
  // takes (server, object, value) before the one signature; more than 50 — the first 50, the rest next time.
  import SignDialog from "./SignDialog.svelte";
  import { errorText, t } from "../lib/i18n.svelte";
  import { BATCH_MAX, acceptable, revocable, runMany, toAccept, type BulkAction } from "../lib/incidentGroups";
  import { MUTES } from "../lib/servers";
  import { session } from "../lib/session.svelte";
  import { toast } from "../lib/toast.svelte";
  import { proposeMany } from "../lib/trust";
  import type { DecisionItem, Incident } from "../lib/types";

  let { items, ondone }: { items: Incident[]; ondone: (keep: string[]) => void } = $props();
  let busy = $state(false);
  let signing = $state<DecisionItem[] | null>(null);
  const now = () => Date.now();
  const live = $derived(items.filter((i) => i.status !== "resolved"));
  const quiet = $derived(live.filter((i) => i.status === "acknowledged" || (i.muted_until && Date.parse(i.muted_until) > now())));
  const accepts = $derived(items.filter(acceptable));
  const batch = $derived(toAccept(items));
  // logins by a key whose access is gone — one bundle of "access revoked", never into the baseline
  const revokes = $derived(toAccept(items, revocable));
  const skipped = $derived(items.filter((i) => !acceptable(i)));
  const why = $derived([...new Set(skipped.map((i) =>
    i.status === "resolved" ? t("mon.closed") : i.accepted ? t("mon.accepted_waiting") : i.check === "logins" ? "logins gap" : i.check))].join(", "));
  const canAccept = $derived(session.can("decide.monitor_trust"));

  async function run(action: BulkAction, targets: Incident[], body: object = {}) {
    busy = true;
    try {
      const got = await runMany(targets.map((i) => i.id), action, body);
      const failed = got.filter((r) => !r.ok);
      if (failed.length) {
        toast(t("mon.bulk.result", { ok: got.length - failed.length, failed: failed.length,
          ids: failed.map((r) => `${r.id}: ${errorText(r.error)}`).join("; ") }), "error");
      }
      ondone(failed.map((r) => r.id));
    } finally {
      busy = false;
    }
  }

  async function accept(kind: "accept" | "revoke" = "accept") {
    busy = true;
    try {
      const ids = kind === "revoke" ? revokes.ids : batch.ids;
      const { items: decisions, refused } = await proposeMany(ids, undefined, undefined, kind);
      if (refused.length) toast(t("mon.bulk.refused", { ids: refused.map((r) => `${r.incident}: ${r.error}`).join("; ") }), "error");
      if (decisions.length) signing = decisions; // what is signed, in words, before the signature
    } catch (exc) {
      toast(errorText(exc), "error");
    } finally {
      busy = false;
    }
  }
</script>

{#if items.length}
  <div class="incident-bulk" role="region" aria-label={t("mon.bulk.selected", { n: items.length })}>
    <span>{t("mon.bulk.selected", { n: items.length })}</span>
    {#if canAccept && accepts.length}
      <button class="btn primary" type="button" disabled={busy} onclick={() => void accept()}>
        {t("mon.bulk.accept", { n: batch.ids.length })}
      </button>
    {/if}
    {#if canAccept && revokes.ids.length}
      <button class="btn" type="button" disabled={busy} onclick={() => void accept("revoke")}>
        {t("mon.bulk.revoke", { n: revokes.ids.length })}
      </button>
    {/if}
    {#if live.length}
      <button class="btn" type="button" disabled={busy} onclick={() => void run("ack", live)}>{t("mon.bulk.ack", { n: live.length })}</button>
      <details>
        <summary class="btn">{t("mon.bulk.mute", { n: live.length })}</summary>
        <div>
          {#each MUTES as span}<button type="button" class="btn small" disabled={busy}
            onclick={() => void run("mute", live, { for: span })}>{t(`incident.mute.${span}`)}</button>{/each}
        </div>
      </details>
    {/if}
    {#if quiet.length}
      <button class="btn" type="button" disabled={busy} onclick={() => void run("unmute", quiet)}>{t("mon.bulk.unmute", { n: quiet.length })}</button>
    {/if}
    {#if canAccept && batch.over}<p class="note">{t("mon.bulk.too_many", { max: BATCH_MAX, n: batch.over })}</p>{/if}
    {#if canAccept && skipped.length}<p class="note">{t("mon.bulk.skipped", { n: skipped.length, why })}</p>{/if}
  </div>
{/if}

{#if signing}
  <SignDialog
    items={signing}
    onclose={() => (signing = null)}
    ondone={() => ((signing = null), ondone([]))}
    onreread={() => ((signing = null), ondone(items.map((i) => i.id)))}
  />
{/if}
