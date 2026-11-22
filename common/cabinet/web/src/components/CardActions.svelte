<script lang="ts">
  // The card's big buttons: exactly the actions the backend lists for this person, the main one
  // highlighted; one that is off, or that the backend explains (the brief is still being written), says why under
  // the buttons. An action that needs a comment asks for it before it is sent; a start sends back the brief version
  // it was shown with (R17); a start whose brief grants factory operations shows them first.
  import { t, type Key } from "../lib/i18n.svelte";
  import type { CardAction } from "../lib/types";
  import ActionComment from "./ActionComment.svelte";
  import FactorySteps from "./FactorySteps.svelte";
  import StagePlan from "./StagePlan.svelte";

  let {
    actions,
    busy = false,
    onact,
  }: {
    actions: CardAction[];
    busy?: boolean;
    onact: (action: string, comment: string | undefined, briefVersion: number | null) => void;
  } = $props();

  let asking = $state<CardAction | null>(null);
  let granting = $state<CardAction | null>(null);
  // the backend's keys: an action's label and its reason are catalog keys (action.*, flow.reason.*)
  const why = (action: CardAction) => t(`flow.reason.${action.reason}` as Key, action.params);
  // …and a start while the brief's council has not ended says what the approval does to it
  const hints = $derived([
    ...actions.filter((a) => a.reason).map((a) => ({ id: a.id, text: why(a) })),
    ...actions.filter((a) => a.council).map((a) => ({ id: `${a.id}:council`, text: t("flow.council_open", a.council) })),
  ]);
  // a label names who is started ("Start @dev"); without an agent the verb stands alone
  const label = (action: CardAction) => t(action.label as Key, { agent: action.agent }).trim();

  function run(action: CardAction, granted = false) {
    if (!granted && (action.operations?.length || action.stages?.length)) granting = action;
    else if (action.comment === "required" || action.comment === "optional") asking = action;
    else onact(action.id, undefined, action.brief_version);
  }

  function grant() {
    const action = granting;
    granting = null;
    if (action) run(action, true);
  }

  function send(text: string) {
    const action = asking;
    asking = null;
    if (action) onact(action.id, text || undefined, action.brief_version);
  }
</script>

{#if actions.length}
  <div class="actions">
    {#each actions as action (action.id)}
      <button
        type="button"
        class="btn"
        class:primary={action.primary}
        disabled={busy || !action.enabled}
        title={action.reason ? why(action) : undefined}
        onclick={() => run(action)}
      >
        {label(action)}
      </button>
    {/each}
  </div>
  {#each hints as hint (hint.id)}
    <p class="hint">{hint.text}</p>
  {/each}
  {#if granting}
    <div class="grant">
      {#if granting.operations?.length}
        <p>{t("flow.grants")}</p>
        <FactorySteps steps={granting.operations} />
      {/if}
      {#if granting.stages?.length}
        <p>{t("flow.stages")}</p>
        {#if granting.chain}<p class="hint">{t("flow.stages_chain")}</p>{/if}
        <StagePlan stages={granting.stages} chain={granting.chain ?? false} />
      {/if}
      <div class="actions">
        <button type="button" class="btn primary" disabled={busy} onclick={grant}>
          {granting.operations?.length ? t("flow.grant") : t("flow.stage")}
        </button>
        <button type="button" class="btn" onclick={() => (granting = null)}>{t("action.cancel")}</button>
      </div>
    </div>
  {/if}
  {#if asking}
    <ActionComment label={t(`flow.${asking.id}.comment` as Key)} optional={asking.comment === "optional"} {busy}
      onsend={send} oncancel={() => (asking = null)} />
  {/if}
{/if}

<style>
  .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .actions .btn { padding: 12px 20px; font-size: 15px; }
  .hint { margin: 0; font-size: 13px; color: var(--muted); }
  .grant { display: grid; gap: 8px; }
  .grant p { margin: 0; font-weight: 500; }
</style>
