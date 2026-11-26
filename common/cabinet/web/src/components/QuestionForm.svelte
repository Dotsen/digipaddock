<script lang="ts">
  // A question form v1: single and multiple choices with a visible label and
  // description, the agent's recommendation marked but never preselected, the person's own words where the
  // question allows them, one shared comment. Up to three questions are one list; a longer round goes step
  // by step and ends with a review. Every choice is a draft of this person, card and form (sessionStorage);
  // a sent answer clears it, a failed one keeps it. A form read from an old text question says so, shows the
  // original and lets the person answer it with free text instead.
  import { isSendKey, readDraft, writeDraft } from "../lib/drafts";
  import { t } from "../lib/i18n.svelte";
  import { answered, emptyAnswers, isShort, missingRequired, summary } from "../lib/questions";
  import type { FormAnswerItem, FormQuestion, QuestionFormV1 } from "../lib/types";
  import Markdown from "./Markdown.svelte";

  let {
    form,
    origin,
    original = "",
    draftKey,
    busy = false,
    onsubmit,
    onfree,
  }: {
    form: QuestionFormV1;
    origin: "agent" | "parsed";
    /** The question's own text (a recognized form shows it on request). */
    original?: string;
    draftKey: string;
    busy?: boolean;
    onsubmit: (answer: { answers: Record<string, FormAnswerItem>; comment: string }) => Promise<boolean>;
    /** A recognized form only: answer with free text instead. */
    onfree?: (text: string) => Promise<boolean>;
  } = $props();

  const uid = $props.id();
  type Draft = { answers: Record<string, FormAnswerItem>; comment: string; step: number; free: string; mode: "form" | "free" };

  /** The draft as far as it still fits the form: unknown questions and options are dropped. */
  function restore(): Draft {
    const draft: Draft = { answers: emptyAnswers(form), comment: "", step: 0, free: "", mode: "form" };
    let raw: any = null;
    try {
      raw = JSON.parse(readDraft(draftKey) || "null");
    } catch {
      raw = null;
    }
    if (!raw || typeof raw !== "object") return draft;
    for (const q of form.questions) {
      const item = raw.answers?.[q.id];
      if (!item || typeof item !== "object") continue;
      const known = new Set(q.options.map((o) => o.id));
      const ids: string[] = Array.isArray(item.selected_option_ids)
        ? item.selected_option_ids.filter((id: unknown) => typeof id === "string" && known.has(id))
        : [];
      draft.answers[q.id] = {
        selected_option_ids: q.type === "single_select" ? ids.slice(0, 1) : ids,
        custom_text: q.type === "text" || q.allow_custom ? String(item.custom_text ?? "") : "",
      };
    }
    draft.comment = typeof raw.comment === "string" ? raw.comment : "";
    draft.free = typeof raw.free === "string" ? raw.free : "";
    draft.mode = raw.mode === "free" && onfree ? "free" : "form";
    draft.step = Number.isInteger(raw.step) ? Math.min(Math.max(raw.step, 0), form.questions.length) : 0;
    return draft;
  }

  const initial = restore();
  let answers = $state(initial.answers);
  let comment = $state(initial.comment);
  let step = $state(initial.step);
  let free = $state(initial.free);
  let mode = $state(initial.mode);
  let sending = $state(false);

  const locked = $derived(busy || sending);
  const short = $derived(isShort(form));
  const total = $derived(form.questions.length);
  const reviewing = $derived(!short && step >= total);
  const shown = $derived(short ? form.questions : reviewing ? [] : [form.questions[step]]);
  const missing = $derived(missingRequired(form, answers));
  const missingList = $derived(missing.map((id) => form.questions.findIndex((q) => q.id === id) + 1).join(", "));

  $effect(() => {
    const value: Draft = { answers: $state.snapshot(answers), comment, step, free, mode };
    const empty =
      !comment && !free && step === 0 && mode === "form" && form.questions.every((q) => !answered(value.answers[q.id]));
    writeDraft(draftKey, empty ? "" : JSON.stringify(value));
  });

  function pick(q: FormQuestion, option: string) {
    const item = answers[q.id];
    if (q.type === "single_select") item.selected_option_ids = [option];
    else if (item.selected_option_ids.includes(option)) item.selected_option_ids = item.selected_option_ids.filter((id) => id !== option);
    else item.selected_option_ids = q.options.map((o) => o.id).filter((id) => id === option || item.selected_option_ids.includes(id));
  }

  async function send() {
    if (locked || missing.length) return;
    sending = true;
    try {
      if (await onsubmit({ answers: $state.snapshot(answers), comment })) writeDraft(draftKey, "");
    } finally {
      sending = false;
    }
  }

  async function sendFree() {
    const text = free.trim();
    if (locked || !text || !onfree) return;
    sending = true;
    try {
      if (await onfree(text)) writeDraft(draftKey, "");
    } finally {
      sending = false;
    }
  }

  // Ctrl/Cmd+Enter sends from the free text, a short form and the review; in a step form it goes on to the next
  // question (the last one to the review), so nothing is sent unseen
  function onKey(event: KeyboardEvent) {
    if (!isSendKey(event)) return; // Enter breaks the line
    event.preventDefault();
    if (mode === "free") void sendFree();
    else if (!short && !reviewing) step += 1;
    else void send();
  }
</script>

<div class="qf">
  {#if origin === "parsed"}
    <p class="parsed muted">{t("question.parsed")}</p>
    <details class="original">
      <summary>{t("question.original")}</summary>
      <Markdown text={original} />
    </details>
  {/if}

  {#if mode === "free"}
    <textarea
      rows="3"
      aria-label={t("decision.reply_placeholder")}
      placeholder={t("decision.reply_placeholder")}
      disabled={locked}
      bind:value={free}
      onkeydown={onKey}
    ></textarea>
    <div class="nav">
      <button class="btn ghost" type="button" disabled={locked} onclick={() => (mode = "form")}>{t("question.by_items")}</button>
      <button class="btn primary" type="button" disabled={locked || !free.trim()} onclick={sendFree}>{t("action.reply")}</button>
    </div>
  {:else}
    {#if !short && !reviewing}<p class="step muted">{t("question.step", { n: step + 1, total })}</p>{/if}
    {#each shown as q (q.id)}
      <fieldset class="q" disabled={locked}>
        <legend>{q.title}{#if q.required}<span class="req"> · {t("question.required")}</span>{/if}</legend>
        {#if q.context}<p class="ctx">{q.context}</p>{/if}
        {#if q.type === "text"}
          <textarea rows="3" aria-label={q.title} bind:value={answers[q.id].custom_text} onkeydown={onKey}></textarea>
        {:else}
          <div class="opts">
            {#each q.options as o (o.id)}
              <label class="opt">
                <input
                  type={q.type === "single_select" ? "radio" : "checkbox"}
                  name="{uid}-{q.id}"
                  value={o.id}
                  checked={answers[q.id].selected_option_ids.includes(o.id)}
                  onchange={() => pick(q, o.id)}
                  onkeydown={onKey}
                />
                <span class="text">
                  <span class="lbl">{o.label}</span>
                  {#if o.recommended}<span class="badge">{t("question.recommended")}</span>{/if}
                  {#if o.description}<span class="desc">{o.description}</span>{/if}
                </span>
              </label>
            {/each}
          </div>
          {#if q.note}<p class="note muted">{q.note}</p>{/if}
          {#if q.allow_custom}
            <textarea
              rows="2"
              aria-label="{q.title} — {t('question.custom')}"
              placeholder={t("question.custom")}
              bind:value={answers[q.id].custom_text}
              onkeydown={onKey}
            ></textarea>
          {/if}
        {/if}
      </fieldset>
    {/each}

    {#if reviewing}
      <ol class="review">
        {#each summary(form, { answers }) as line, i (i)}
          <li>
            <span class="title">{line.title}</span>
            <span class:muted={!line.labels.length && !line.custom}>
              {[line.labels.join("; "), line.custom].filter(Boolean).join(" — ") || t("question.no_answer")}
            </span>
            <button class="btn small ghost" type="button" disabled={locked} onclick={() => (step = i)}>{t("question.change")}</button>
          </li>
        {/each}
      </ol>
    {/if}

    {#if short || reviewing}
      <textarea
        class="comment"
        rows="2"
        aria-label={t("question.comment")}
        placeholder={t("question.comment")}
        disabled={locked}
        bind:value={comment}
        onkeydown={onKey}
      ></textarea>
      {#if missing.length}<p class="missing">{t("question.missing", { list: missingList })}</p>{/if}
    {/if}

    <div class="nav">
      {#if onfree}
        <button class="btn ghost" type="button" disabled={locked} onclick={() => (mode = "free")}>{t("question.free")}</button>
      {/if}
      {#if !short && step > 0}
        <button class="btn" type="button" disabled={locked} onclick={() => (step -= 1)}>{t("question.back")}</button>
      {/if}
      {#if !short && !reviewing}
        <button class="btn" type="button" disabled={locked} onclick={() => (step += 1)}>
          {t(step === total - 1 ? "question.review" : "question.next")}
        </button>
      {:else}
        <button class="btn primary" type="button" disabled={locked || missing.length > 0} onclick={send}>{t("question.submit")}</button>
      {/if}
    </div>
  {/if}
</div>

<style>
  .qf { padding: 0 var(--sp-14) var(--sp-12); display: grid; gap: var(--sp-10); }
  .parsed, .step, .note, .ctx { margin: 0; font-size: var(--fs-md); }
  .ctx { white-space: pre-wrap; }
  .original summary { cursor: pointer; color: var(--muted); font-size: var(--fs-md); }
  .q { border: 1px solid var(--line); border-radius: var(--r-md); padding: var(--sp-8) var(--sp-10) var(--sp-10); margin: 0; display: grid; gap: var(--sp-8); }
  .q legend { font-weight: 600; padding: 0 var(--sp-4); }
  .req { font-weight: 400; color: var(--muted); font-size: var(--fs-sm); }
  .opts { display: grid; gap: var(--sp-6); }
  .opt { display: flex; gap: var(--sp-8); align-items: flex-start; cursor: pointer; }
  .opt input { margin-top: 3px; }
  .text { display: flex; flex-wrap: wrap; gap: var(--sp-4) var(--sp-8); align-items: baseline; }
  .lbl { font-weight: 500; }
  .badge { font-size: var(--fs-xs); padding: var(--sp-2) var(--sp-8); border-radius: var(--r-pill); border: 1px solid var(--good); color: var(--good); }
  .desc { flex-basis: 100%; color: var(--muted); font-size: var(--fs-md); }
  textarea { width: 100%; resize: vertical; box-sizing: border-box; }
  .review { margin: 0; padding-left: var(--sp-20); display: grid; gap: var(--sp-6); }
  .review li { display: flex; gap: var(--sp-8); flex-wrap: wrap; align-items: baseline; }
  .review .title { font-weight: 600; }
  .missing { margin: 0; color: var(--warn); font-size: var(--fs-md); }
  .nav { display: flex; gap: var(--sp-8); flex-wrap: wrap; justify-content: flex-end; }
</style>
