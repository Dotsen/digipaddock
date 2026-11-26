<script lang="ts">
  // The card's next step: what happens now, who does it, what stands in the way — in one
  // place. The step itself is the backend's (`card.next`, the one the agents' snapshot names); the page
  // words it and shows its decision's controls — BOSS's proposal is approved here (the card's own start is not
  // offered beside it), a question, a PR or another decision links to itself. A request on its way says how far
  // it got, and each of the backend's actions says what it does and who does it (an off one says why and stays).
  // The page adds no transitions or rules of its own. The pipeline's records come first: a problem —
  // work that stopped with nobody on it — the outcome the holder recorded and what it waits for, blocking and not.
  import { ApiError } from "../lib/api";
  import { approveDecisions, BLOCKED_RETRY_MS, withPasskey } from "../lib/decide";
  import { errorText, hasKey, t, type Key } from "../lib/i18n.svelte";
  import { session } from "../lib/session.svelte";
  import { age, dayTime } from "../lib/time";
  import { toast } from "../lib/toast.svelte";
  import type { AgentSession, Card, CardAction, CardPipeline, Command, DecisionItem } from "../lib/types";
  import CardActions from "./CardActions.svelte";
  import DeferControl from "./DeferControl.svelte";
  import RunState from "./RunState.svelte";
  import StagePlan, { gateText } from "./StagePlan.svelte";

  let {
    card,
    actions,
    decisions,
    commands,
    sessions,
    busy = false,
    now = Date.now(),
    canControl = false,
    onact,
    onchanged,
    oncontinue,
  }: {
    card: Card;
    actions: CardAction[];
    decisions: Record<string, DecisionItem>;
    /** Newest first. */
    commands: Command[];
    sessions: AgentSession[];
    busy?: boolean;
    now?: number;
    canControl?: boolean;
    onact: (action: string, comment: string | undefined, briefVersion: number | null) => void;
    /** A decision was taken or found changed: the page reads the card and its decisions again. */
    onchanged: () => void;
    oncontinue?: (session: string) => void;
  } = $props();

  const open = $derived(Object.values(decisions).filter((i) => i.decision.state === "open"));
  const promote = $derived(open.find((i) => i.decision.type === "promote"));
  const mayPromote = $derived(
    !!promote && session.can(promote.decision.decide_permission, promote.decision.project || undefined),
  );
  // RunState: the agent's turn ended on the person's answer (an agent's question, dev's PR)
  const waiting = $derived(
    open.some((i) => (i.decision.type === "question" && i.decision.requested_by.startsWith("agent:")) || i.decision.type === "pr_approve"),
  );
  // a promote no approval can give any more is a hint, never the lead that hides what the card waits for
  const deadPromote = $derived(
    !!promote && mayPromote && !promote.decision.step_up && !!(promote.view.stale || promote.view.brief_stale),
  );
  // BOSS is off for now (a release reinstalls it): no button the board would refuse; read again until it
  // is back
  const blocked = $derived(promote?.view.blocked ?? "");
  $effect(() => {
    if (!blocked) return;
    const id = setInterval(onchanged, BLOCKED_RETRY_MS);
    return () => clearInterval(id);
  });
  // the proposal's approval starts the card: its own start would be the same step twice
  const shown = $derived(promote ? actions.filter((a) => a.id !== "start") : actions);
  const next = $derived(card.next ?? null);
  const step = $derived(next?.decision && (next.what === "asked" || next.what === "wait_decision") ? next.decision : null);
  const agentOf = $derived(next?.params.agent || card.assignee);
  // the words of a step with nothing to control; a problem, stages, help, a run and a closed card say themselves
  const LEADS: Record<string, Key> = {
    done: "next.done", brief_writing: "next.brief_writing", brief_ready: "next.step.brief_ready",
    brief_failed: "next.step.brief_failed", inbox: "next.step.inbox", review: "next.step.review",
    blocked_unexplained: "next.step.blocked_unexplained", no_request: "next.step.no_request",
    turn_ended: "next.step.turn_ended", stage_review: "next.step.stage_review", stage_gate: "next.step.stage_gate",
    council_running: "next.step.council_running", council_writer: "next.step.council_writer",
  };
  // the words' parameters: the step's own (a council's round, its critics, its findings), without the empty ones
  const leadParams = $derived.by(() => {
    const out: Record<string, string | number> = { agent: agentOf, who: next?.who ?? "", version: "" };
    for (const [name, value] of Object.entries(next?.params ?? {})) {
      if (value !== null && value !== undefined && value !== "") out[name] = value;
    }
    return out;
  });
  const typeName = (type: string) => (hasKey(`decision.type.${type}`) ? t(`decision.type.${type}` as Key) : type);
  const toDecision = (type: string): Key =>
    type === "question" ? "next.to_question" : type === "pr_approve" ? "next.to_pr" : type === "promote" ? "next.open_proposal" : "next.to_decision";
  const explained = $derived(
    shown
      .filter((a) => a.enabled)
      .map((a) => ({ id: a.id, key: `next.${a.label.replace(/^action\./, "")}`, agent: a.agent }))
      .filter((a) => hasKey(a.key)),
  );
  const link = (id: string) => `/cards/${encodeURIComponent(card.id)}?decision=${id}`;
  const pipeline = $derived(card.pipeline ?? null);
  // an aggregate of stages: its children move it, nobody is waited for on the card itself
  const staged = $derived(
    pipeline?.stages && pipeline.stages.done < pipeline.stages.total && card.status !== "Done" && card.status !== "Cancelled"
      ? pipeline.stages
      : null,
  );
  const stageItems = $derived(
    (staged?.items ?? []).map((i) => ({
      key: i.key, title: i.title, role: i.role, depends: [], child: i.child, status: i.status, gate: i.gate, reason: i.reason,
    })),
  );
  const blocking = $derived(pipeline?.waits.filter((w) => w.blocking) ?? []);
  const free = $derived(pipeline?.waits.filter((w) => !w.blocking) ?? []);
  // on the coordinator's own card nobody but the owner sorts its problem out
  const by = $derived(card.assignee === "@boss" ? t("next.problem.by_owner") : t("next.problem.by_boss"));
  type Wait = CardPipeline["waits"][number];
  // an operation has no page of its own: its wait says what it installs and how far it got
  const waitHref = (w: Wait) =>
    w.kind === "decision" ? link(w.ref) : w.kind === "card" ? `/cards/${encodeURIComponent(w.ref)}` : null;
  // the card's help: the open requests with their step, and the last ended one with how it ended
  const OPEN_HELP = ["requested", "assigned", "accepted"];
  const helps = $derived.by(() => {
    const all = (pipeline?.assists ?? []).filter((a) => a.state !== "gone");
    return [...all.filter((a) => OPEN_HELP.includes(a.state)), ...all.filter((a) => !OPEN_HELP.includes(a.state)).slice(0, 1)];
  });
  function helpLabel(a: NonNullable<CardPipeline["assists"]>[number]): string {
    const reason = a.reason && hasKey(`assist.reason.${a.reason}`) ? ` (${t(`assist.reason.${a.reason}` as Key)})` : "";
    return `${a.helper || a.role} · ${t(`assist.state.${a.state}` as Key)}${reason}`;
  }

  function waitLabel(w: Wait): string {
    if (w.kind === "card") return w.ref;
    if (w.kind === "contour") return t("next.wait.contour", { name: w.ref }); // released by root
    if (w.kind === "time") return t("next.wait.time", { when: dayTime(w.ref, session.me?.timezone) });
    if (w.kind === "council") return t("next.wait.council"); // the turn waits for the critics' round
    if (w.kind === "assist") {
      const a = w.assist;
      return [`${t("next.wait.assist")} ${a?.helper || a?.role || ""}`.trim(), t(`assist.state.${a?.state ?? "gone"}` as Key)].join(" · ");
    }
    if (w.kind === "operation") {
      const op = w.operation;
      const state = t(`next.operation.${op?.state ?? "pending"}` as Key);
      const head = [t("next.wait.operation"), op?.component || (op?.agents ?? []).join(", ")].filter(Boolean).join(" ");
      const done = op?.done.length && op.state === "running" ? t("next.wait.operation_done", { agents: op.done.join(", ") }) : "";
      return [head, state, done].filter(Boolean).join(" · ");
    }
    const type = decisions[w.ref]?.decision.type;
    return t(type === "question" ? "next.wait.question" : type === "pr_approve" ? "next.wait.pr" : "next.wait.decision");
  }

  let approving = $state(false);
  async function approve() {
    if (!promote || approving) return;
    approving = true;
    try {
      await approveDecisions([promote], withPasskey);
      onchanged();
    } catch (exc) {
      toast(errorText(exc), "error");
      if (exc instanceof ApiError && [409, 403, 503].includes(exc.status)) onchanged();
    } finally {
      approving = false;
    }
  }
</script>

<section class="next" aria-label={t("next.title")}>
  <h2>{t("next.title")}</h2>
  {#if pipeline}
    {#each pipeline.problems as problem (problem.kind + problem.created)}
      <p class="problem warn">
        {t(`next.problem.${problem.kind}` as Key, { agent: problem.agent || card.assignee })} — {by} · {age(problem.created, now)}
      </p>
    {/each}
    {#if pipeline.outcome}
      <p class="outcome">
        {t(`next.outcome.${pipeline.outcome.outcome}` as Key, { agent: pipeline.outcome.agent })}{#if pipeline.outcome.note}: {pipeline.outcome.note}{/if}
        {#if pipeline.outcome.state !== "final"}<span class="hint"> ({t(`next.outcome_state.${pipeline.outcome.reason === "stopped" ? "stopped" : pipeline.outcome.state}` as Key)})</span>{/if}
      </p>
    {/if}
    {#each [{ name: t("next.waits.blocking"), list: blocking }, { name: t("next.waits.non_blocking"), list: free }] as group (group.name)}
      {#if group.list.length}
        <div class="waits">
          <span class="hint">{group.name}:</span>
          <ul aria-label={group.name}>
            {#each group.list as w (w.id)}
              <li>{#if waitHref(w)}<a class="chip" href={waitHref(w)}>{waitLabel(w)}</a>{:else}<span class="chip">{waitLabel(w)}</span>{/if}{#if w.state === "pending"}<span class="hint"> {t("next.wait.after_turn")}</span>{/if}</li>
            {/each}
          </ul>
        </div>
      {/if}
    {/each}
    {#if staged}
      <p class="lead">{t("next.stages", { done: staged.done, total: staged.total })}</p>
      <StagePlan stages={stageItems} compact chain={staged.chain ?? false} />
    {/if}
    {#if helps.length}
      <div class="waits">
        <span class="hint">{t("next.assists")}:</span>
        <ul aria-label={t("next.assists")}>
          {#each helps as a (a.id)}
            <li><span class="chip">{helpLabel(a)}</span> <span class="hint">{a.step}</span></li>
          {/each}
        </ul>
      </div>
    {/if}
  {/if}
  {#if step}
    {@const asked = next?.what === "asked"}
    {#if asked}
      <p class="lead">{t("next.asked", { agent: agentOf })}</p>
    {/if}
    {#if step.type === "promote" && promote?.decision.id === step.id}
      {#if deadPromote}
        <!-- said below: the proposal no approval can give any more -->
      {:else if !mayPromote}
        {#if !asked}<p class="lead">{t("next.promote_other")}</p>{/if}
      {:else if promote.decision.step_up}
        {#if !asked}<p class="lead">{t("next.promote")}</p>{/if}
        <a class="go" href={link(step.id)}>{t("next.open_proposal")}</a>
      {:else if blocked}
        {#if !asked}<p class="lead">{t("next.promote")}</p>{/if}
        <p class="hint" role="status">{t(`decision.blocked.${blocked}` as Key)}</p>
      {:else}
        {#if !asked}<p class="lead">{t("next.promote")}</p>{/if}
        <button type="button" class="btn primary" disabled={busy || approving} onclick={() => void approve()}>
          {t("action.start_brief")}
        </button>
        <p class="hint">{t("next.promote_hint", { agent: promote.view.assignee || card.assignee })}</p>
      {/if}
    {:else}
      {#if !asked}
        <p class="lead">
          {#if step.type === "question"}{t("next.question", { agent: agentOf })}
          {:else if step.type === "pr_approve"}{t("next.pr", { number: next?.params.number ?? "" })}
          {:else if step.type === "promote"}{t("next.promote")}
          {:else}{t("next.step.decision", { type: typeName(step.type) })}{/if}
        </p>
      {/if}
      <a class="go" href={link(step.id)}>{t(toDecision(step.type))}</a>
    {/if}
  {:else if next?.what === "wait_deps" && next.source === "derived"}
    <p class="lead">{t("next.blocked")}</p>
    {#if card.dependencies.length}
      <p class="deps">{#each card.dependencies as dep (dep)}<a class="chip" href="/cards/{dep}">{dep}</a>{/each}</p>
    {/if}
  {:else if next?.what === "not_started"}
    <p class="lead">{next.who !== "-" ? t("next.wait", { who: next.who }) : t("next.nobody")}</p>
  {:else if next && LEADS[next.what]}
    <p class="lead">{t(LEADS[next.what], leadParams)}</p>
    {#if next.what === "brief_writing"}<p class="hint">{t("next.brief_writing_hint")}</p>{/if}
  {/if}
  {#if pipeline?.stage}
    <!-- a stage of a chain plan: its plan, and why it waits for the owner when BOSS did not decide on it -->
    {#if pipeline.stage.gate === "held"}<p class="hint">{gateText("held", pipeline.stage.reason)}</p>{/if}
    <p class="hint">{t("next.stage_of")} <a class="chip" href="/cards/{encodeURIComponent(pipeline.stage.parent)}">{pipeline.stage.parent}</a></p>
  {/if}
  {#if promote && deadPromote}
    <p class="hint">{t("next.promote_stale")}</p>
    <a class="go" href={link(promote.decision.id)}>{t("next.open_proposal")}</a>
  {/if}

  <RunState {commands} {sessions} {waiting} {canControl} {now} {oncontinue} pauses={pipeline?.pauses ?? []} />
  <DeferControl {card} {onchanged} />

  {#if card.status === "Cancelled"}
    <!-- closed without the work, and why; the canonical card is where the work goes on -->
    <p class="lead">
      {t("status.cancelled")} ·
      {#if card.outcome === "duplicate" && card.canonical}
        {t("card.outcome.duplicate", { canonical: "" })}<a class="chip" href="/cards/{card.canonical}">{card.canonical}</a>
      {:else}
        {t("card.outcome.cancelled")}
      {/if}
    </p>
  {/if}
  {#if shown.length}
    <CardActions actions={shown} {busy} {onact} />
    {#each explained as line (line.id)}
      <p class="hint">{t(line.key as Key, { agent: line.agent || card.assignee })}</p>
    {/each}
  {/if}
</section>

<style>
  .next { display: flex; flex-direction: column; gap: var(--sp-8); padding: var(--sp-12) var(--sp-14); border: 1px solid var(--line); border-radius: var(--r-lg); background: var(--surface); min-width: 0; }
  h2 { margin: 0; font-size: var(--fs-xs); text-transform: uppercase; letter-spacing: .08em; color: var(--muted); font-weight: 600; }
  .lead { margin: 0; font-weight: 600; overflow-wrap: anywhere; }
  .hint { margin: 0; font-size: var(--fs-md); color: var(--muted); }
  .go { color: var(--accent); font-size: var(--fs-ui); }
  .deps { margin: 0; display: flex; gap: var(--sp-6); flex-wrap: wrap; }
  .deps .chip { text-decoration: none; color: var(--accent); }
  .problem { margin: 0; font-weight: 600; overflow-wrap: anywhere; }
  .problem.warn { color: var(--warn); }
  .outcome { margin: 0; overflow-wrap: anywhere; }
  .waits { display: flex; gap: var(--sp-6); align-items: baseline; flex-wrap: wrap; }
  .waits ul { display: flex; gap: var(--sp-6); flex-wrap: wrap; margin: 0; padding: 0; list-style: none; }
  .waits .chip { text-decoration: none; color: var(--accent); }
</style>
