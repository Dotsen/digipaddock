<script module lang="ts">
  import type { AuthorKind } from "./Avatar.svelte";

  export function authorKind(author: string): AuthorKind {
    if (author.startsWith("user:")) return "owner";
    if (author === "agent:boss") return "boss";
    if (author.startsWith("agent:")) return "agent";
    return "monitor";
  }
</script>

<script lang="ts">
  // One thread message by kind: text (a bubble tinted by its author), progress (a quiet line), a BOSS
  // directive, a status, decision or diagnostic line (catalog text; R31: what kd-diag checked for whom), a notice
  // of a root service or an executor (catalog text, edged by its level), a file.
  // Tool calls are grouped by Thread. A message's files follow its text: a raster picture as a thumbnail
  // that opens full screen, audio (a voice message) as a player with its transcription or its place in the
  // line under it, anything else as a download, a deleted one as «file deleted»; its author or the owner
  // deletes a file here, and retries a transcription that failed on the way.
  import { apiDelete, apiPost } from "../lib/api";
  import { filesOf, isAudio, isPreview, retryRefusal, textOf, voiceView, type Attachment } from "../lib/attachments";
  import { fullDuration } from "../lib/audioDuration";
  import { bytes } from "../lib/bytes";
  import { errorText, hasKey, t, type Key } from "../lib/i18n.svelte";
  import { thread } from "../lib/stores/thread.svelte";
  import { toast } from "../lib/toast.svelte";
  import { summary } from "../lib/questions";
  import { reasonText } from "../lib/runReason";
  import { session } from "../lib/session.svelte";
  import { stamp, when } from "../lib/time";
  import type { DecisionItem, Message } from "../lib/types";
  import Avatar from "./Avatar.svelte";
  import ActivityBlock from "./ActivityBlock.svelte";
  import Markdown from "./Markdown.svelte";

  let { message, decision }: { message: Message; decision?: DecisionItem } = $props();

  const kind = $derived(authorKind(message.author));
  const mine = $derived(message.author === `user:${session.me?.id}`);
  const system = $derived(["status", "decision", "diagnostic"].includes(message.kind));

  // what a decision line says (an answer, a reason): short and on one line it stays in the line, else it is a
  // block under it that keeps its lines; a form's answer reads question by question
  const INLINE_MAX = 80;
  const said = $derived(message.kind === "decision" ? message.body : "");
  const inline = $derived(!!said && !said.includes("\n") && said.length <= INLINE_MAX);
  const answer = $derived.by(() => {
    const form = decision?.view?.form;
    const given = decision?.decision.answer;
    if (!said || !form || !given || decision?.decision.reply !== said) return null;
    const rows = summary(form, given).map((row) => ({
      title: row.title,
      value: [row.labels.join("; "), row.custom].filter(Boolean).join(" — ") || t("question.no_answer"),
    }));
    return { rows, comment: given.comment?.trim() ?? "" };
  });

  const attached = $derived(filesOf(message));
  const text = $derived(textOf(message));
  const deletable = $derived(mine || session.can("*"));
  let opened = $state<Attachment | null>(null);
  let deleting = $state("");

  async function remove(file: Attachment) {
    if (deleting || !confirm(t("files.delete_confirm", { name: file.name }))) return;
    const generation = thread.generation;
    deleting = file.id;
    try {
      thread.add(await apiDelete(`/api/files/${file.id}`), generation);
    } catch (exc) {
      toast(errorText(exc), "error");
    } finally {
      deleting = "";
    }
  }

  // "Retry": the author or the owner puts an audio whose transcription failed back in the line
  let retrying = $state("");
  async function retry(file: Attachment) {
    if (retrying) return;
    const generation = thread.generation;
    retrying = file.id;
    try {
      thread.add(await apiPost(`/api/files/${file.id}/transcribe`), generation);
    } catch (exc) {
      toast(retryRefusal(exc), "error");
    } finally {
      retrying = "";
    }
  }

  function onKey(event: KeyboardEvent) {
    if (opened && event.key === "Escape") opened = null;
  }

  // a help result: who did the step, how it ended, that the agent reports it, the limit of its check
  const help = $derived.by(() => {
    if (message.kind !== "assist") return null;
    const p = message.payload ?? {};
    const reason = p.reason && hasKey(`assist.reason.${p.reason}`) ? ` (${t(`assist.reason.${p.reason}` as Key)})` : "";
    const head = [
      t("thread.assist.title"),
      t(`assist.state.${p.state === "failed" ? "failed" : "done"}` as Key) + reason,
      t("thread.assist.reported", { agent: message.author_name }),
      ...(p.late ? [t("thread.assist.late")] : []),
    ].join(" · ");
    return { head, limit: typeof p.limit === "string" && p.limit ? t("thread.assist.limit", { limit: p.limit }) : "" };
  });

  function line(m: Message): string {
    const key = m.payload?.key;
    if (typeof key !== "string" || !hasKey(key)) return m.body;
    const params: Record<string, string> = { ...(m.payload.params ?? {}) };
    if (params.type && hasKey(`decision.type.${params.type}`)) params.type = t(`decision.type.${params.type}` as Key);
    if (key === "sys.session.failed" && params.error) params.error = reasonText(params.error);
    if (m.kind === "diagnostic") {
      if (params.category && hasKey(`diag.category.${params.category}`)) params.category = t(`diag.category.${params.category}` as Key);
      if (params.time) params.time = when(params.time);
    }
    return t(key, params);
  }

</script>

{#snippet fileList()}
<div class="files">
  {#each attached as file (file.id)}
    <div class="att">
      {#if file.deleted}
        <span class="muted gone">{file.name ? `${file.name} — ` : ""}{t("files.deleted")}</span>
      {:else if isAudio(file)}
        {@const spoken = voiceView(file)}
        <span class="voice">
          <span class="player">
            <audio use:fullDuration controls preload="metadata" src="/api/files/{file.id}?inline=1" aria-label={t("voice.message", { name: file.name })}></audio>
            <a class="muted" href="/api/files/{file.id}" download={file.name} title={file.name}>{bytes(Number(file.size ?? 0))}</a>
          </span>
          {#if spoken?.text}
            <span class="spoken" aria-label={t("voice.transcript")}>{spoken.text}</span>
          {:else if spoken}
            <span class="spoken muted" class:failed={spoken.failed}>
              {spoken.line}
              {#if spoken.retry && deletable}
                <button type="button" class="btn small ghost" disabled={retrying === file.id} onclick={() => void retry(file)}>{t("voice.retry")}</button>
              {/if}
            </span>
          {/if}
        </span>
      {:else if isPreview(file)}
        <button type="button" class="thumb" title={file.name} aria-label={t("files.open", { name: file.name })} onclick={() => (opened = file)}>
          <img src="/api/files/{file.id}?inline=1" alt={file.name} loading="lazy" />
        </button>
      {:else}
        <a class="file" href="/api/files/{file.id}" download={file.name}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 11l-8.5 8.5a5 5 0 0 1-7-7L14 4a3.5 3.5 0 0 1 5 5l-8.5 8.5a2 2 0 0 1-3-3L15 7" /></svg>
          {file.name} <span class="muted">{bytes(Number(file.size ?? 0))}</span>
        </a>
      {/if}
      {#if deletable && !file.deleted}
        <button type="button" class="del" disabled={!!deleting} title={t("files.delete")} aria-label={t("files.delete")} onclick={() => void remove(file)}>×</button>
      {/if}
    </div>
  {/each}
</div>
{/snippet}

{#if message.kind === "status" && ["sys.session.instructions_selected", "sys.session.instructions_loaded", "sys.session.memory"].includes(message.payload?.key) && ["instructions", "memory"].includes(message.payload?.activity?.type)}
  <ActivityBlock {message} />
{:else if message.kind === "notice"}
  <div class="msg notice {message.payload?.level === 'critical' ? 'crit' : message.payload?.level === 'warn' ? 'warn' : 'info'}">
    <div class="body">{line(message)}<span class="muted" title={when(message.created)}> · {stamp(message.created)}</span></div>
  </div>
{:else if system}
  <div class="msg sys">
    <div class="body">
      {line(message)}{#if inline}: «{said}»{/if}
      <span class="muted" title={when(message.created)}> · {stamp(message.created)}</span>
    </div>
    {#if said && !inline}
      {#if answer}
        <div class="answer">
          <ol>
            {#each answer.rows as row, i (i)}<li><span class="q">{row.title}</span><span class="a">{row.value}</span></li>{/each}
          </ol>
          {#if answer.comment}<p class="comment">{answer.comment}</p>{/if}
        </div>
      {:else}
        <div class="answer text">{said}</div>
      {/if}
    {/if}
    {#if attached.length}{@render fileList()}{/if}
  </div>
{:else if message.kind === "progress"}
  <div class="msg progress">
    <span class="dot" aria-hidden="true"></span>
    <span><b>{message.author_name}</b> · <Markdown text={message.body} inline /></span>
  </div>
{:else}
  <div class="msg {mine ? 'owner' : kind}">
    <Avatar name={message.author_name} kind={mine ? "owner" : kind} />
    <div class="content">
      <div class="who">
        <b>{message.author_name}</b>
        {#if message.kind === "directive"}<span class="chip boss">{t("card.directive")}</span>{/if}
        <span title={when(message.created)}>{stamp(message.created)}</span>
      </div>
      <div class="b">
        {#if help}<div class="help">{help.head}</div>{/if}
        {#if text}<Markdown text={text} />{/if}
        {#if attached.length}{@render fileList()}{/if}
        {#if !text && !attached.length}<Markdown text={message.body} />{/if}
        {#if help?.limit}<div class="help limit">{help.limit}</div>{/if}
      </div>
    </div>
  </div>
{/if}

<svelte:window onkeydown={onKey} />
{#if opened}
  <div class="viewer" role="dialog" aria-modal="true" aria-label={opened.name}>
    <button type="button" class="shade" aria-label={t("files.close")} onclick={() => (opened = null)}></button>
    <img src="/api/files/{opened.id}?inline=1" alt={opened.name} />
    <div class="bar">
      <a href="/api/files/{opened.id}" download={opened.name}>{opened.name}</a>
      <button type="button" class="btn small ghost" onclick={() => (opened = null)}>{t("files.close")}</button>
    </div>
  </div>
{/if}

<style>
  .msg { display: grid; grid-template-columns: 30px minmax(0, 1fr); gap: 10px; align-items: start; }
  .content { min-width: 0; }
  .who { font-size: 12px; color: var(--muted); margin-bottom: 4px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .who b { color: var(--text); font-weight: 600; }
  .b { border-radius: 12px; padding: 9px 12px; background: var(--surface); max-width: 68ch; }
  .agent .b { background: var(--au-agent); border-bottom-left-radius: 4px; }
  .boss .b { background: var(--au-boss); border-bottom-left-radius: 4px; }
  .monitor .b { background: var(--au-monitor); border-bottom-left-radius: 4px; }
  .msg.owner { grid-template-columns: minmax(0, 1fr) 30px; }
  .msg.owner :global(.avatar) { order: 2; }
  .msg.owner .content { justify-self: end; display: flex; flex-direction: column; align-items: flex-end; max-width: 100%; }
  .owner .b { background: var(--au-owner); border-bottom-right-radius: 4px; }
  .help { font-size: 12px; color: var(--muted); margin-bottom: 4px; }
  .help.limit { margin: 4px 0 0; }
  .msg.progress { display: flex; gap: 8px; align-items: baseline; color: var(--muted); font-size: 13px; padding-left: 40px; }
  .msg.progress .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); flex: none; transform: translateY(-1px); }
  .msg.progress b { font-weight: 500; color: var(--text); }
  .msg.notice { grid-template-columns: 1fr; }
  .msg.notice .body { white-space: pre-line; font-size: 13px; padding: 6px 10px; border-left: 3px solid var(--line); background: var(--surface); border-radius: 6px; }
  .msg.notice.warn .body { border-left-color: var(--warn); }
  .msg.notice.crit .body { border-left-color: var(--crit); }
  .msg.sys { grid-template-columns: 1fr; }
  .msg.sys .body { text-align: center; font-size: 12px; color: var(--muted); }
  .answer { justify-self: center; width: 100%; max-width: 68ch; box-sizing: border-box; font-size: 13px; background: var(--surface); border-left: 3px solid var(--accent); border-radius: 6px; padding: 8px 12px; overflow-wrap: anywhere; }
  .answer.text { white-space: pre-line; }
  .answer ol { margin: 0; padding-left: 20px; display: grid; gap: 6px; }
  .answer .q { display: block; font-size: 12px; color: var(--muted); }
  .answer .a { display: block; }
  .answer .comment { margin: 8px 0 0; white-space: pre-line; }
  .file { text-decoration: none; display: inline-flex; gap: 6px; align-items: center; }
  .file svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
  .files { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
  .files:first-child { margin-top: 0; }
  .att { display: inline-flex; align-items: flex-start; gap: 4px; max-width: 100%; }
  .thumb { padding: 0; border: 1px solid var(--line); border-radius: 8px; background: var(--bg); cursor: zoom-in; overflow: hidden; display: block; }
  .thumb img { display: block; max-width: 240px; max-height: 180px; object-fit: contain; }
  .voice { display: inline-flex; flex-direction: column; gap: 4px; max-width: 100%; }
  .voice .player { display: inline-flex; gap: 8px; align-items: center; max-width: 100%; }
  .voice audio { width: 280px; max-width: 100%; height: 40px; }
  .voice a { font-size: 12px; text-decoration: none; }
  .spoken { font-size: 13px; white-space: pre-line; overflow-wrap: anywhere; max-width: 60ch; }
  .spoken.muted { font-size: 12px; display: inline-flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .spoken.failed { color: var(--warn); }
  .del { background: none; border: 0; padding: 0 2px; color: var(--muted); cursor: pointer; font-size: 15px; line-height: 1; }
  .del:hover { color: var(--crit); }
  .gone { font-style: italic; }
  .viewer { position: fixed; inset: 0; z-index: 100; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; padding: 16px; }
  .viewer .shade { position: absolute; inset: 0; background: rgb(0 0 0 / 0.85); border: 0; cursor: zoom-out; }
  .viewer img { position: relative; max-width: 100%; max-height: calc(100% - 48px); object-fit: contain; pointer-events: none; }
  .viewer .bar { position: relative; display: flex; gap: 12px; align-items: center; color: #fff; }
  .viewer .bar a { color: #fff; }
</style>
