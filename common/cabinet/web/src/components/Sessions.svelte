<script lang="ts">
  // The agents' sessions: a card's Sessions tab or an agent's page. Each row is the agent, its
  // state, when it started, when it was last heard from and its error; whoever may steer the agent
  // (agents.control) stops a live session and continues an ended one. Below them, the card's worker jobs
  // — specialization, state, slot, cost and the reason of a failure; BOSS cancels a job, not this page.
  // Whoever may steer the agent also opens a session's turns on "Turn" (SessionTurns, loaded once).
  import { apiPost } from "../lib/api";
  import { errorText, t } from "../lib/i18n.svelte";
  import { reasonText } from "../lib/runReason";
  import { age, when } from "../lib/time";
  import { toast } from "../lib/toast.svelte";
  import { ENDED_SESSION, LIVE_SESSION, type AgentSession, type WorkerJob } from "../lib/types";
  import SessionTurns from "./SessionTurns.svelte";

  let {
    sessions,
    jobs = [],
    canControl,
    showAgent = true,
    onchanged,
  }: {
    sessions: AgentSession[];
    jobs?: WorkerJob[];
    canControl: boolean;
    showAgent?: boolean;
    onchanged?: () => void;
  } = $props();

  let busy = $state<string | null>(null);
  let opened = $state<Record<string, boolean>>({}); // mounted once: hiding keeps what was loaded
  let shown = $state<Record<string, boolean>>({});

  function toggle(id: string) {
    opened[id] = true;
    shown[id] = !shown[id];
  }
  const stateText = (state: string) => t(`sessions.state.${state}` as Parameters<typeof t>[0]);
  const jobState = (state: string) => t(`sessions.job.state.${state}` as Parameters<typeof t>[0]);
  const cost = (job: WorkerJob) =>
    Object.values(job.usage?.models ?? {}).reduce((sum, model) => sum + (model.cost || 0), 0).toFixed(2);

  async function act(session: AgentSession, what: "continue" | "stop") {
    busy = session.id;
    try {
      await apiPost(`/api/sessions/${encodeURIComponent(session.id)}/${what}`);
      onchanged?.();
    } catch (exc) {
      toast(errorText(exc), "error");
    } finally {
      busy = null;
    }
  }
</script>

{#if sessions.length === 0 && jobs.length === 0}
  <p class="empty small">{t("sessions.empty")}</p>
{:else if sessions.length > 0}
  <ul class="sessions">
    {#each sessions as session (session.id)}
      <li>
        <span class="head">
          {#if showAgent}<b>@{session.agent}</b>{/if}
          <span class="chip" class:good={session.state === "working"} class:crit={session.state === "failed"}>
            {stateText(session.state)}
          </span>
          {#if canControl && LIVE_SESSION.includes(session.state)}
            <button type="button" class="btn small ghost" disabled={busy === session.id} onclick={() => act(session, "stop")}>{t("sessions.stop")}</button>
          {:else if canControl && ENDED_SESSION.includes(session.state)}
            <button type="button" class="btn small" disabled={busy === session.id} onclick={() => act(session, "continue")}>{t("sessions.continue")}</button>
          {/if}
          {#if canControl}
            <button type="button" class="btn small ghost turns-btn" aria-expanded={!!shown[session.id]} onclick={() => toggle(session.id)}>{t("sessions.turns.open")}</button>
          {/if}
        </span>
        <span class="meta">
          {#if session.started}<span>{t("sessions.started", { when: when(session.started) })}</span>{/if}
          {#if session.heartbeat}<span>{t("sessions.heartbeat", { age: age(session.heartbeat) })}</span>{/if}
          <!-- a process on a cabinet model, or on an effort over the agent's -->
          {#if session.model}<span>{t("sessions.model", { model: session.model })}</span>{/if}
          {#if session.effort}<span>{t("sessions.effort", { effort: session.effort })}</span>{/if}
        </span>
        {#if session.error}<span class="error" title={session.error}>{reasonText(session.error)}</span>{/if}
        {#if opened[session.id]}
          <div hidden={!shown[session.id]}><SessionTurns session={session.id} /></div>
        {/if}
      </li>
    {/each}
  </ul>
{/if}

{#if jobs.length > 0}
  <h4 class="jobs-title">{t("sessions.jobs.title")}</h4>
  <ul class="sessions">
    {#each jobs as job (job.id)}
      <li>
        <span class="head">
          {#if showAgent}<b>@{job.agent}</b>{/if}
          <span>{t("sessions.job.line", { spec: job.spec, state: jobState(job.state), slot: job.slot || "—", cost: cost(job) })}</span>
        </span>
        <span class="meta">
          {#if job.started}<span>{t("sessions.started", { when: when(job.started) })}</span>{/if}
        </span>
        {#if job.reason && job.state !== "done"}
          <span class="error" title={job.reason}>{reasonText(job.reason, job.reason)}</span>
        {/if}
      </li>
    {/each}
  </ul>
{/if}

<style>
  .sessions { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
  .sessions li { background: var(--surface); border: 1px solid var(--line-soft); border-radius: 10px; padding: 8px 12px; display: grid; gap: 4px; font-size: 14px; }
  .head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .head .btn:first-of-type { margin-left: auto; }
  .meta { display: flex; gap: 4px 12px; flex-wrap: wrap; font-size: 12px; color: var(--muted); }
  .error { font-size: 13px; color: var(--crit); overflow-wrap: anywhere; }
  .small { padding: 8px 0; }
  .jobs-title { margin: 16px 0 8px; font-size: 14px; font-weight: 600; }
</style>
