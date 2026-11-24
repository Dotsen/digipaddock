<script lang="ts">
  // One decision as the page shows it: what it asks by type (a plan's steps, a critical command, a BOSS
  // proposal with the card's description and AC, a question), its term, and — for whoever may decide —
  // approve/sign, ask, reject or reply; once decided: who, when, how it was signed, and revoke — folded to that
  // line: the old document opens on "Details", a linked decision opens unfolded.
  // A pull request is signed with the mode the person picks — approve, merge, merge and release — or
  // answered with remarks: a text and remarks on lines of its diff.
  import { untrack } from "svelte";
  import { ApiError, apiPost } from "../lib/api";
  import { approveDecisions, BLOCKED_RETRY_MS, withPasskey } from "../lib/decide";
  import { REMARKS_MAX, remarksText, type LineComment } from "../lib/diff";
  import { draftKey, isSendKey, readDraft, writeDraft } from "../lib/drafts";
  import { errorText, t, type Key } from "../lib/i18n.svelte";
  import { summary } from "../lib/questions";
  import { prodEffect } from "../lib/repo";
  import { checkName, incidentHref, serverHref } from "../lib/servers";
  import { applyLine, trustTitle } from "../lib/trust";
  import { session } from "../lib/session.svelte";
  import { projects } from "../lib/stores/projects.svelte";
  import { remaining, when } from "../lib/time";
  import { toast } from "../lib/toast.svelte";
  import type { DecisionItem, FormAnswerItem, PrAnswer, PrMode } from "../lib/types";
  import Markdown from "./Markdown.svelte";
  import FactorySteps from "./FactorySteps.svelte";
  import PlanSteps, { riskKey } from "./PlanSteps.svelte";
  import PrBody from "./PrBody.svelte";
  import QuestionForm from "./QuestionForm.svelte";
  import SignDialog from "./SignDialog.svelte";

  let {
    item,
    onchanged = () => {},
    highlight = false,
    onreply,
  }: {
    item: DecisionItem;
    onchanged?: () => void;
    highlight?: boolean;
    /** In a thread: «reply» picks this question as the target of the thread's field. */
    onreply?: (id: string) => void;
  } = $props();

  const REVOCABLE = ["admin_plan", "admin_critical", "delegate"];
  const d = $derived(item.decision);
  const v = $derived(item.view);
  const open = $derived(d.state === "open");
  // blocked for now (BOSS is off while a release reinstalls it) — no approval offered, read again until it
  // is back
  const blocked = $derived(open && !!v.blocked);
  $effect(() => {
    if (!blocked) return;
    const id = setInterval(onchanged, BLOCKED_RETRY_MS);
    return () => clearInterval(id);
  });
  const mayDecide = $derived(session.can(d.decide_permission, d.project || undefined));
  const question = $derived(d.type === "question");
  const trust = $derived(d.type === "monitor_trust"); // no card — its P-n, server or address
  // a question wakes the agent that asked for the decision, on its card: without either nobody reads it
  const askable = $derived(!!d.card && d.requested_by.startsWith("agent:"));
  const tone = $derived(
    !open ? "done" : d.type === "admin_critical" ? "crit" : question ? "q" : "",
  );
  const left = $derived(open ? remaining(d.expires) : "");
  const title = $derived(
    question
      ? t("decision.type.question")
      : trust
        ? trustTitle({ ...d.payload, incident: v.incident ?? d.payload.incident })
        : v.intent || v.title || v.card?.title || d.card,
  );
  const outcome = $derived<Key>(
    d.state === "approved"
      ? question
        ? "decision.state.answered"
        : "decision.state.approved"
      : (`decision.state.${d.state}` as Key),
  );
  const pr = $derived(d.type === "pr_approve");
  // the mode a PR was signed with is in the signature's binding (what kd-review checked); a signature carried to a
  // new head keeps the mode of the head it was given on
  const signedMode = $derived<PrMode | undefined>(d.signature?.binding?.mode ?? v.carried?.mode);
  const outcomeText = $derived(
    pr && d.state === "rejected"
      ? t("decision.pr.remarks_sent")
      : pr && d.state === "approved" && signedMode
        ? `${t(outcome)} · ${t(`decision.pr.done.${signedMode}` as Key)}`
        : t(outcome),
  );
  // one approval: merge, or merge and release with the tick where the project is released from here (the owner,
  // 19.10: approve-only is not offered; the API still takes it)
  let releaseToo = $state(false);
  let comments = $state<LineComment[]>([]);
  let signMode = $state<PrMode | undefined>(undefined);

  let expanded = $state(untrack(() => highlight));
  const full = $derived(open || expanded);
  let mode = $state<"" | "ask" | "reject">("");
  let busy = $state(false);
  let signing = $state(false);
  // the answer's text is a draft of this person, this decision and this kind of answer
  const draft = $derived(
    draftKey(session.me?.id ?? "", d.card, `decision:${d.id}:${question ? "reply" : mode || "none"}`),
  );
  let texts = $state<Record<string, string>>({});
  const text = $derived(texts[draft] ?? readDraft(draft));
  const remarksLength = $derived(remarksText(text, comments).length); // what the thread records, held to its limit
  function setText(value: string) {
    texts[draft] = value;
    writeDraft(draft, value);
  }

  // a plan of test environments is approved by the cabinet's rule, not by a person
  const who = (actor: string) =>
    actor === "rule:test-env"
      ? t("decision.rule.test_env")
      : actor === `user:${session.me?.id}` ? session.me?.name : actor.replace(/^(user|agent):/, "@");

  // changed (409), refused (403: someone answered first, a stale brief, the right was taken) or not for now (503: BOSS
  // is off): the page shows what the decision is now before anyone acts on it again
  const reread = (exc: unknown) => exc instanceof ApiError && [409, 403, 503].includes(exc.status);

  async function act(work: () => Promise<unknown>) {
    if (busy) return;
    busy = true;
    try {
      await work();
      setText("");
      mode = "";
      onchanged();
    } catch (exc) {
      toast(errorText(exc), "error");
      if (reread(exc)) onchanged();
    } finally {
      busy = false;
    }
  }

  const answer = (kind: "ask" | "reject" | "reply") =>
    act(() => apiPost(`/api/decisions/${d.id}/${kind}`, { text: text.trim() }));
  const agree = () => act(() => apiPost(`/api/decisions/${d.id}/reply`, { text: t("action.agree") }));

  // a question with a form: the agent's own, or the one its old text reads as
  const form = $derived(question ? (v.form ?? null) : null);
  const formKey = $derived(draftKey(session.me?.id ?? "", d.card, `form:${d.id}:${v.form_sha256 ?? ""}`));
  // what was chosen, over the same form the answer was given to; otherwise its readable text
  const chosen = $derived(form && d.answer && d.answer.form_sha256 === v.form_sha256 ? summary(form, d.answer) : null);

  /** One answer to the question; false keeps the person's draft (a 409 or 403: the question is read again). */
  async function replyWith(body: Record<string, unknown>): Promise<boolean> {
    try {
      await apiPost(`/api/decisions/${d.id}/reply`, body);
      onchanged();
      return true;
    } catch (exc) {
      toast(errorText(exc), "error");
      if (reread(exc)) onchanged();
      return false;
    }
  }
  const replyForm = (p: { answers: Record<string, FormAnswerItem>; comment: string }) =>
    replyWith({ ...p, form_sha256: v.form_sha256 });
  const replyFree = (value: string) => replyWith({ text: value });

  function approve() {
    signMode = undefined;
    if (d.step_up) signing = true;
    else void act(() => approveDecisions([item], withPasskey));
  }

  function signPr(chosen: PrMode) {
    signMode = chosen;
    signing = true;
  }

  // the remarks are recorded in the thread first (they wake dev); Forgejo gets them as a review after that
  async function sendRemarks() {
    const answer = await apiPost<PrAnswer>(`/api/decisions/${d.id}/reject`, { text: text.trim(), comments });
    comments = [];
    if (answer.forge === "failed") toast(t("decision.pr.remarks_forge_failed"), "error");
  }

  function submit(event?: SubmitEvent) {
    event?.preventDefault();
    if (busy) return;
    if (pr) {
      if ((text.trim() || comments.length) && remarksLength <= REMARKS_MAX) void act(sendRemarks);
      return;
    }
    if (!text.trim()) return;
    void answer(question ? "reply" : mode === "ask" ? "ask" : "reject");
  }

  function onKey(event: KeyboardEvent) {
    if (!isSendKey(event)) return; // Enter breaks the line
    event.preventDefault();
    submit();
  }
</script>

<article class="dec {tone}" class:highlight id="decision-{d.id}">
  <div class="head">
    <div class="ttl">
      <span>{title}</span>
      <span class="chip">{t(`decision.type.${d.type}` as Key)}</span>
      {#if v.production}<span class="chip crit">{t("decision.production")}</span>{/if}
    </div>
    <div class="sub">
      {t("decision.requested_by", { who: who(d.requested_by) ?? "" })}
      {#if left}· {t("decision.expires_in", { time: left })}{/if}
      {#if open && d.step_up}· {t("decision.needs_signature")}{/if}
    </div>
  </div>

  {#if full}
  {#if blocked}
    <p class="warn" role="status">{t(`decision.blocked.${v.blocked}` as Key)}</p>
  {:else if open && (v.stale || v.problem)}
    <p class="warn">{v.stale ? t(pr ? "decision.pr.stale" : "decision.stale") : ""} {v.brief_stale ? t("flow.reason.brief_stale") : (v.problem ?? "")}</p>
  {/if}

  {#if pr}
    {#if v.carried}
      <p class="hint">{t("decision.pr.carried", { from: v.carried.head.slice(0, 7), target: v.target ?? "" })}</p>
    {/if}
    <PrBody id={d.id} card={d.card} view={v} commenting={open && mayDecide && mode === "reject"} bind:comments {onchanged} />
  {:else if d.type === "admin_plan"}
    {#if v.basis}<p class="body"><b>{t("decision.basis")}:</b> {v.basis}</p>{/if}
    <PlanSteps steps={v.steps ?? []} />
  {:else if d.type === "admin_critical"}
    <div class="body grid">
      <div class="row">
        {#each v.servers ?? [] as server (server)}<span class="chip">{server}</span>{/each}
        <span class="chip crit">{t("decision.once")}</span>
      </div>
      {#if v.basis}<p><b>{t("decision.basis")}:</b> {v.basis}</p>{/if}
      <pre class="command">{v.command}</pre>
      {#if v.effect}<p><b>{t("decision.effect")}:</b> {v.effect}</p>{/if}
      {#if v.risk}<p><b>{t("decision.risk")}:</b> <span class="risk {v.risk.level}">{t(riskKey(v.risk.level))}</span> — {v.risk.why}</p>{/if}
      {#if v.rollback}<p><b>{t("decision.rollback")}:</b> {v.rollback}</p>{/if}
    </div>
  {:else if d.type === "promote" || d.type === "delegate"}
    <div class="body grid">
      {#if v.reason}<p><b>{t("decision.reason")}:</b> {v.reason}</p>{/if}
      {#if v.revision}<p class="muted">{t("card.revision", { revision: v.revision })} · {projects.name(v.project ?? "")} · {v.assignee}</p>{/if}
      {#if v.description}<Markdown text={v.description} />{/if}
      {#if v.acceptance?.length}
        <ul class="ac">
          {#each v.acceptance as ac, i (i)}
            <li><span aria-hidden="true">{ac.checked ? "☑" : "☐"}</span> <Markdown text={ac.text} inline /></li>
          {/each}
        </ul>
      {/if}
      {#if v.operations?.length}
        <p><b>{t("brief.operations")}</b></p>
        <FactorySteps steps={v.operations} />
      {/if}
      {#if v.scope}
        <p>{t("decision.scope", { children: v.scope.max_children, active: v.scope.max_active, days: v.scope.days })}</p>
        <p class="muted">{t("decision.scope_projects", { projects: v.scope.projects.map((p) => projects.name(p)).join(", ") })}</p>
      {/if}
    </div>
  {:else if trust}
    <div class="body grid">
      {#if v.incident}
        <p>
          <a href={incidentHref(v.incident.id)}>{v.incident.id}</a> · {v.incident.check ? checkName(v.incident.check, v.incident.subject) : ""}
          {#if v.incident.value}: {v.incident.value}{/if}
        </p>
      {/if}
      {#if v.kind === "baseline" && v.server}
        <p>{#if v.server === "all"}{t("trust.all")}{:else}<a href={serverHref(v.server)}>{v.server}</a>{/if}</p>
      {/if}
      {#if v.kind === "trust"}<p><code>{v.cidr}</code></p>{/if}
      {#if v.label}<p><b>{t("trust.add.label")}:</b> {v.label}</p>{/if}
      <p class="muted">{t("trust.proposed_by", { who: who(v.proposed_by || d.requested_by) ?? "" })}</p>
      {#if v.text}<p><b>{t("trust.why")}:</b> {v.text}</p>{/if}
    </div>
  {:else if question && form && open && mayDecide}
    {#if form.text}<div class="body"><Markdown text={form.text} /></div>{/if}
  {:else if question}
    <div class="body"><Markdown text={v.text ?? d.payload.text ?? ""} /></div>
    {#if form && v.origin === "agent" && open}
      <ol class="body titles">{#each form.questions as q (q.id)}<li>{q.title}</li>{/each}</ol>
    {/if}
  {/if}

  {#if d.question}<p class="body muted">{t("decision.asked", { agent: who(d.requested_by) ?? "", text: d.question })}</p>{/if}
  {/if}

  {#if open && mayDecide && pr}
    {#if mode === "reject"}
      <form class="qa remarks" onsubmit={submit}>
        <textarea
          rows="3"
          value={text}
          oninput={(event) => setText(event.currentTarget.value)}
          onkeydown={onKey}
          placeholder={t("decision.pr.remarks_placeholder")}
        ></textarea>
        <p class="muted hint">{t("decision.pr.remarks_lines", { count: comments.length })}</p>
        <p class="hint" class:muted={remarksLength <= REMARKS_MAX} class:error-text={remarksLength > REMARKS_MAX}>
          {t("decision.pr.remarks_length", { n: remarksLength, max: REMARKS_MAX })}
        </p>
        <button
          class="btn danger"
          type="submit"
          disabled={busy || (!text.trim() && !comments.length) || remarksLength > REMARKS_MAX}
        >
          {t("decision.pr.remarks_send")}
        </button>
      </form>
    {/if}
    <div class="act">
      {#if !v.stale}
        <button
          class="btn primary"
          type="button"
          disabled={busy}
          onclick={() => signPr(releaseToo && v.release === true ? "release" : "merge")}
        >
          {t(prodEffect(v.effect) ? "action.pr_merge_prod" : "action.pr_merge")}
        </button>
        {#if v.release === true}
          <label class="tick"><input type="checkbox" bind:checked={releaseToo} disabled={busy} />{t("decision.pr.release_too")}</label>
          {#if (v.plan_pulls ?? 0) > 1}<p class="hint">{t("decision.pr.release_plan")}</p>{/if}
        {/if}
      {/if}
      <button class="btn danger" type="button" disabled={busy} onclick={() => (mode = mode === "reject" ? "" : "reject")}>
        {t("action.pr_remarks")}
      </button>
    </div>
  {:else if open && mayDecide}
    {#if question && form}
      <QuestionForm
        {form}
        origin={v.origin === "parsed" ? "parsed" : "agent"}
        original={v.text ?? ""}
        draftKey={formKey}
        {busy}
        onsubmit={replyForm}
        onfree={v.origin === "parsed" ? replyFree : undefined}
      />
    {:else if question && onreply}
      <div class="act">
        <button class="btn primary" type="button" disabled={busy} onclick={() => onreply(d.id)}>{t("action.reply")}</button>
        {#if v.expect !== "file"}<button class="btn" type="button" disabled={busy} onclick={agree}>{t("action.agree")}</button>{/if}
      </div>
    {:else if question || mode}
      <form class="qa" class:question onsubmit={submit}>
        <textarea
          rows="3"
          value={text}
          oninput={(event) => setText(event.currentTarget.value)}
          onkeydown={onKey}
          placeholder={t(question ? "decision.reply_placeholder" : mode === "ask" ? "decision.ask_placeholder" : "decision.reject_placeholder")}
        ></textarea>
        <button class="btn {mode === 'reject' ? 'danger' : 'primary'}" type="submit" disabled={busy || !text.trim()}>
          {t(question ? "action.reply" : mode === "ask" ? "action.send" : "action.reject")}
        </button>
        {#if question && v.expect !== "file"}<button class="btn" type="button" disabled={busy} onclick={agree}>{t("action.agree")}</button>{/if}
      </form>
    {/if}
    {#if !question}
      <div class="act">
        {#if !v.stale && !blocked}
          <button class="btn primary" type="button" disabled={busy} onclick={approve}>
            {t(d.step_up ? "action.sign" : "action.approve")}
          </button>
        {/if}
        {#if askable}
          <button class="btn" type="button" disabled={busy} onclick={() => (mode = mode === "ask" ? "" : "ask")}>{t("action.ask")}</button>
        {/if}
        <button class="btn danger" type="button" disabled={busy} onclick={() => (mode = mode === "reject" ? "" : "reject")}>{t("action.reject")}</button>
      </div>
    {/if}
  {:else if !open}
    <div class="result">
      <span>
        {outcomeText} · {t("decision.by", { who: who(d.decided_by) ?? "", when: when(d.decided_at) })}
        {#if d.signature}· {d.signature.type === "totp" ? t("decision.signed_totp") : t("decision.signed_passkey")}{/if}
      </span>
      {#if trust && d.state === "approved" && d.apply_state}
        <span class="apply {d.apply_state}" role="status">{applyLine(d.apply_state, d.apply_detail, d.applied_at)}</span>
      {/if}
      {#if chosen}
        <ol class="chosen">
          {#each chosen as line, i (i)}
            <li>
              <span class="title">{line.title}</span>
              <span class:muted={!line.labels.length && !line.custom}>
                {[line.labels.join("; "), line.custom].filter(Boolean).join(" — ") || t("question.no_answer")}
              </span>
            </li>
          {/each}
          {#if d.answer?.comment}<li class="comment">{d.answer.comment}</li>{/if}
        </ol>
      {:else if d.reply && d.state !== "revoked"}<!-- a withdrawal's reply is the system's note to the agent -->
        <span class="muted">{t("decision.answer", { text: d.reply })}</span>
      {/if}
      <button type="button" class="btn small ghost" aria-expanded={expanded} onclick={() => (expanded = !expanded)}>
        {t(expanded ? "decision.less" : "decision.more")}
      </button>
      {#if d.state === "approved" && mayDecide && REVOCABLE.includes(d.type)}
        <button class="btn danger" type="button" disabled={busy} onclick={() => act(() => apiPost(`/api/decisions/${d.id}/revoke`))}>
          {t("action.revoke")}
        </button>
      {/if}
    </div>
  {/if}
</article>

{#if signing}
  <SignDialog
    items={[item]}
    mode={signMode}
    onclose={() => (signing = false)}
    ondone={() => ((signing = false), onchanged())}
    onreread={() => ((signing = false), onchanged())}
  />
{/if}

<style>
  .dec { border: 1px solid var(--line); border-left: 3px solid var(--warn); border-radius: var(--radius); background: var(--surface); overflow: hidden; }
  .dec.crit { border-left-color: var(--crit); border-color: var(--crit); }
  .dec.q { border-left-color: var(--info); }
  .dec.done { border-left-color: var(--good); }
  .dec.highlight { box-shadow: 0 0 0 2px var(--accent); }
  .head { padding: 12px 14px 8px; display: flex; flex-direction: column; gap: 6px; }
  .ttl { font-weight: 700; font-size: 15px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .sub { color: var(--muted); font-size: 13px; }
  .warn { margin: 0 14px 10px; padding: 8px 10px; border-radius: 8px; background: var(--warn-bg); color: var(--warn); font-size: 13px; }
  .body { padding: 0 14px 10px; margin: 0; font-size: 14px; }
  .body b { font-weight: 600; }
  .grid { display: grid; gap: 6px; }
  .grid p { margin: 0; }
  .command { margin: 0; padding: 8px 10px; background: var(--bg); border-radius: 6px; font-size: 12.5px; white-space: pre-wrap; overflow-wrap: anywhere; }
  .risk.low { color: var(--good); } .risk.medium { color: var(--warn); } .risk.high { color: var(--crit); }
  .ac { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
  .titles { padding-left: 34px; }
  .chosen { flex-basis: 100%; margin: 0; padding-left: 20px; display: grid; gap: 4px; color: var(--text); font-weight: 400; }
  .chosen .title { font-weight: 600; margin-right: 6px; }
  .chosen .comment { list-style: none; margin-left: -20px; white-space: pre-wrap; }
  .qa { padding: 0 14px 12px; display: flex; gap: 8px; align-items: flex-end; }
  .qa textarea { flex: 1; min-width: 0; resize: vertical; min-height: 64px; }
  .qa.question { flex-wrap: wrap; }
  .qa.question textarea { flex-basis: 100%; }
  .remarks { flex-direction: column; align-items: stretch; }
  .remarks .hint { margin: 0; font-size: 13px; }
  .remarks button { align-self: flex-start; }
  .act { border-top: 1px solid var(--line); padding: 10px 14px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; background: var(--surface-2); }
  .result { border-top: 1px solid var(--line); padding: 10px 14px; background: var(--surface-2); color: var(--good); font-size: 13px; font-weight: 500; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; justify-content: space-between; }
  .tick { display: inline-flex; gap: 6px; align-items: center; font-size: 13px; cursor: pointer; }
  .apply { flex-basis: 100%; font-weight: 400; color: var(--muted); }
  .apply.applied { color: var(--good); }
  .apply.error { color: var(--crit); }
  .body a { color: var(--accent); text-decoration: none; }
</style>
