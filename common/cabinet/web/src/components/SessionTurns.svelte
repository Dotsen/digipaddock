<script lang="ts">
  // A session's turns, for whoever controls the agent: each turn is the command that started it, how it
  // went and its cost; open it for its steps — the agent's text, each tool call with its result (✓/✗, size) and,
  // when the host kept it, the tool's masked output on "Output". Loaded once when the row's "Turn" opens it.
  import { onMount } from "svelte";
  import { apiGet } from "../lib/api";
  import { bytes } from "../lib/bytes";
  import { statusParams } from "../lib/contextLimit";
  import { errorText, hasKey, t, type Key } from "../lib/i18n.svelte";
  import { reasonText } from "../lib/runReason";
  import { models } from "../lib/stores/models.svelte";
  import { when } from "../lib/time";
  import { toast } from "../lib/toast.svelte";
  import type { SessionTurn, ToolOutput, TurnStep } from "../lib/types";

  let { session }: { session: string } = $props();

  let turns = $state<SessionTurn[] | null>(null);
  let failed = $state(false);
  let outputs = $state<Record<number, ToolOutput | "loading">>({});

  const base = () => `/api/sessions/${encodeURIComponent(session)}`;

  async function load() {
    failed = false;
    try {
      turns = await apiGet<SessionTurn[]>(`${base()}/turns`);
    } catch {
      failed = true;
    }
  }

  async function output(seq: number) {
    if (outputs[seq]) {
      delete outputs[seq];
      return;
    }
    outputs[seq] = "loading";
    try {
      outputs[seq] = await apiGet<ToolOutput>(`${base()}/output/${seq}`);
    } catch (exc) {
      delete outputs[seq];
      toast(errorText(exc), "error");
    }
  }

  onMount(load);

  type Row =
    | { type: "text"; step: TurnStep }
    | { type: "tool"; step: TurnStep; result: TurnStep | undefined }
    | { type: "status"; step: TurnStep };

  /** A call and its result on one line (paired by the call's id); a result with no call stands alone. */
  function rows(steps: TurnStep[]): Row[] {
    const results = new Map(steps.filter((s) => s.kind === "tool_result" && s.id).map((s) => [s.id, s]));
    const paired = new Set(steps.filter((s) => s.kind === "tool_call" && s.id && results.has(s.id)).map((s) => s.id));
    const out: Row[] = [];
    for (const step of steps) {
      if (step.kind === "tool_call") out.push({ type: "tool", step, result: step.id ? results.get(step.id) : undefined });
      else if (step.kind === "tool_result" && !paired.has(step.id)) out.push({ type: "tool", step, result: step });
      else if (step.kind === "status") out.push({ type: "status", step });
      else if (step.kind === "text") out.push({ type: "text", step });
    }
    return out;
  }

  const kindText = (kind: string) => {
    const key = `sessions.turns.kind.${kind}`;
    return hasKey(key) ? t(key) : kind;
  };
  const stateText = (state: string) => {
    const key = `sessions.turns.state.${state}`;
    return hasKey(key) ? t(key) : state;
  };
  const statusText = (step: TurnStep) =>
    step.status && hasKey(step.status.key)
      ? t(step.status.key as Key, statusParams(step.status.key, step.status.params))
      : step.body;
</script>

{#if failed}
  <p class="note error">
    {t("sessions.turns.failed")}
    <button type="button" class="btn small ghost" onclick={load}>{t("run.retry")}</button>
  </p>
{:else if turns === null}
  <p class="note">{t("sessions.turns.loading")}</p>
{:else if turns.length === 0}
  <p class="note">{t("sessions.turns.empty")}</p>
{:else}
  <ul class="turns">
    {#each turns as turn (turn.command)}
      <li>
        <details class="turn">
          <summary>
            <b>{kindText(turn.kind)}</b>
            <span class="text" title={turn.text}>{turn.text}</span>
            <span class="meta">{when(turn.created)}</span>
            {#if turn.state === "done"}
              <span class="ok">✓</span>
            {:else if turn.state === "failed"}
              <span class="bad">✗</span>
              {#if turn.error}<span class="bad" title={turn.error}>{reasonText(turn.error)}</span>{/if}
            {:else}
              <span class="meta">{stateText(turn.state)}</span>
            {/if}
            {#if turn.served?.length}
              <span class="meta served" title={t("sessions.turns.served", { names: turn.served.join(", ") })}>{models.served(turn.served)}</span>
            {/if}
            <span class="meta">${turn.cost.toFixed(2)}</span>
          </summary>
          {#if turn.steps.length === 0}
            <p class="note">{t("sessions.turns.no_steps")}</p>
          {:else}
            <ol class="steps">
              {#each rows(turn.steps) as row (row.step.seq)}
                <li class="step {row.type}">
                  {#if row.type === "text"}
                    <span class="say">{row.step.body}</span>
                  {:else if row.type === "status"}
                    <span class="meta">{statusText(row.step)}</span>
                  {:else}
                    <code>{row.step.kind === "tool_call" ? row.step.body : row.step.tool || "tool"}</code>
                    {#if row.result}
                      <span class={row.result.ok ? "ok" : "bad"}>{row.result.ok ? "✓" : "✗"}</span>
                      {#if row.result.size !== null}<span class="meta">{bytes(row.result.size)}</span>{/if}
                      {#if row.result.output}
                        {@const seq = row.result.seq}
                        {@const shown = outputs[seq]}
                        <button type="button" class="link" aria-expanded={!!shown} onclick={() => output(seq)}>
                          {t("sessions.turns.output")}
                        </button>
                        {#if shown && shown !== "loading"}
                          <pre class="output">{shown.text}</pre>
                          {#if shown.truncated}<span class="meta">{t("sessions.turns.truncated")}</span>{/if}
                        {/if}
                      {/if}
                    {/if}
                  {/if}
                </li>
              {/each}
            </ol>
          {/if}
        </details>
      </li>
    {/each}
  </ul>
{/if}

<style>
  .turns { list-style: none; margin: var(--sp-4) 0 0; padding: 0; display: grid; gap: var(--sp-4); }
  .turn summary { cursor: pointer; display: flex; gap: var(--sp-4) var(--sp-8); flex-wrap: wrap; align-items: baseline; font-size: var(--fs-md); }
  .turn summary .text { min-width: 0; overflow-wrap: anywhere; font-family: monospace; font-size: var(--fs-sm); }
  .steps { list-style: none; margin: var(--sp-6) 0 var(--sp-4); padding: 0 0 0 var(--sp-12); display: grid; gap: var(--sp-4); border-left: 2px solid var(--line-soft); }
  .step { display: flex; gap: var(--sp-4) var(--sp-8); flex-wrap: wrap; align-items: baseline; font-size: var(--fs-md); }
  .step .say { white-space: pre-wrap; overflow-wrap: anywhere; }
  .step code { overflow-wrap: anywhere; font-size: var(--fs-sm); }
  .output { flex-basis: 100%; margin: var(--sp-2) 0 0; padding: var(--sp-8); max-height: 320px; overflow: auto; background: var(--surface-2); border: 1px solid var(--line-soft); border-radius: var(--r-md); font-size: var(--fs-sm); white-space: pre; }
  .meta, .note { font-size: var(--fs-sm); color: var(--muted); }
  .note { margin: var(--sp-4) 0; }
  .ok { color: var(--good); }
  .bad, .error { color: var(--crit); }
  .link { background: none; border: none; padding: 0; color: var(--accent); cursor: pointer; font-size: var(--fs-sm); text-decoration: underline; }
</style>
