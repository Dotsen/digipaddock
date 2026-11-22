<script lang="ts">
  // A markdown field (a card's description): read view, or a textarea with preview. The draft
  // keeps the text and the base it started from (text and revision) per person and card, so it survives
  // leaving the card; saving sends the base revision. When the card's text changed under the draft, Save
  // goes away and the person chooses: take the new text, or put theirs over it. A new revision with the same
  // text only moves the base.
  import { untrack } from "svelte";
  import { readDraft, writeDraft } from "../lib/drafts";
  import { t } from "../lib/i18n.svelte";
  import Markdown from "./Markdown.svelte";

  let {
    value,
    revision,
    draftKey,
    editable,
    onsave,
  }: {
    value: string;
    revision: number;
    draftKey: string;
    editable: boolean;
    onsave: (text: string, revision: number) => Promise<boolean>;
  } = $props();

  interface Base {
    text: string;
    revision: number;
  }

  function restore(): { text: string; base: Base } | null {
    try {
      const saved = JSON.parse(readDraft(draftKey) || "null");
      if (typeof saved?.text === "string" && typeof saved?.base?.text === "string" && Number.isInteger(saved?.base?.revision)) {
        return saved;
      }
    } catch {
      // not a draft of this editor
    }
    return null;
  }

  const saved = untrack(() => (editable ? restore() : null)); // a draft opens the editor once, on mount
  let editing = $state(saved !== null);
  let preview = $state(false);
  let draft = $state(saved?.text ?? "");
  let base = $state<Base>(saved?.base ?? { text: "", revision: 0 });
  let busy = $state(false);
  const conflict = $derived(editing && revision !== base.revision && value !== base.text);

  function keep() {
    writeDraft(draftKey, editing ? JSON.stringify({ text: draft, base }) : "");
  }

  // the card moved on but its text is the one the draft started from: nothing to choose
  $effect(() => {
    if (editing && revision !== base.revision && value === base.text) {
      base = { text: value, revision };
      keep();
    }
  });

  function start() {
    draft = value;
    base = { text: value, revision };
    preview = false;
    editing = true;
    keep();
  }

  function stop() {
    editing = false;
    keep();
  }

  async function save(over = false) {
    busy = true;
    try {
      // over: the person chose to put their text over the newer one
      if (await onsave(draft, over ? revision : base.revision)) stop();
    } finally {
      busy = false;
    }
  }

  function takeNew() {
    draft = value;
    base = { text: value, revision };
    keep();
  }
</script>

{#if editing}
  <div class="editor">
    {#if preview}
      <div class="preview"><Markdown text={draft} /></div>
    {:else}
      <textarea
        value={draft}
        oninput={(event) => ((draft = event.currentTarget.value), keep())}
        rows="10"
        aria-label={t("card.description")}
      ></textarea>
    {/if}
    {#if conflict}
      <div class="conflict" role="alert">
        <p class="note">{t("editor.changed")}</p>
        <div class="preview"><Markdown text={value} /></div>
        <div class="row">
          <button class="btn" type="button" disabled={busy} onclick={takeNew}>{t("editor.take_new")}</button>
          <button class="btn danger" type="button" disabled={busy} onclick={() => save(true)}>{t("editor.save_mine")}</button>
        </div>
      </div>
    {/if}
    <div class="row">
      {#if !conflict}
        <button class="btn primary" type="button" disabled={busy} onclick={() => save()}>{t("action.save")}</button>
      {/if}
      <button class="btn" type="button" onclick={() => (preview = !preview)}>{t(preview ? "action.edit" : "action.preview")}</button>
      <button class="btn" type="button" disabled={busy} onclick={stop}>{t("action.cancel")}</button>
    </div>
  </div>
{:else}
  <div class="view">
    {#if value.trim()}<Markdown text={value} />{:else}<p class="muted">{t("card.description_empty")}</p>{/if}
    {#if editable}<button class="btn" type="button" onclick={start}>{t("action.edit")}</button>{/if}
  </div>
{/if}

<style>
  .editor, .view { display: grid; gap: 10px; justify-items: start; }
  .editor textarea, .preview, .conflict { width: 100%; }
  .preview { background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; min-height: 90px; }
  .conflict { display: grid; gap: 8px; padding: 10px; border-radius: 8px; background: var(--warn-bg); }
  .note { margin: 0; color: var(--warn); font-weight: 500; }
  .view p { margin: 0; }
</style>
