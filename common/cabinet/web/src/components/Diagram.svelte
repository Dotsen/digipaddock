<script lang="ts">
  // A ```mermaid block of a text, drawn in the diagram frame (lib/diagram.ts): sandboxed, so the drawing
  // code has no part of the cabinet's session. Until the frame answers the block is one quiet line; a text mermaid
  // cannot draw, or a frame that stays silent, shows the block's source and why.
  import { ANSWER_MS, FRAME, answerOf, renderMessage } from "../lib/diagram";
  import { t } from "../lib/i18n.svelte";

  let { code }: { code: string } = $props();
  let frame: HTMLIFrameElement | undefined = $state();
  let loaded = $state(false);
  let height = $state(0);
  let failed: string | null = $state(null); // why there is no drawing; null while there is no failure
  let waiting: ReturnType<typeof setTimeout> | undefined;
  let asked = 0; // the number of the last request: an answer about an earlier text is not an answer

  // the block's text goes to the frame once it has loaded, and again when the text changes
  $effect(() => {
    const text = code;
    if (!frame) return;
    if (loaded) frame.contentWindow?.postMessage(renderMessage(++asked, text, frame), "*"); // an opaque origin has no name
    waiting = setTimeout(() => (failed = t("diagram.silent")), ANSWER_MS);
    return () => clearTimeout(waiting);
  });

  function answered(event: MessageEvent) {
    if (!frame || event.source !== frame.contentWindow) return;
    const answer = answerOf(event.data);
    if (!answer || answer.id !== asked) return;
    clearTimeout(waiting);
    if (answer.type === "rendered") {
      height = answer.height;
      failed = null;
    } else {
      failed = answer.message.split("\n")[0].replace(/:$/, ""); // a parse error's first line; the rest repeats the text
    }
  }
</script>

<svelte:window onmessage={answered} />

<div class="diagram">
  {#if failed !== null}
    <div class="state">{t("diagram.failed")}{failed ? ` · ${failed}` : ""}</div>
    <pre><code>{code}</code></pre>
  {:else if !height}
    <div class="state">{t("diagram.drawing")}</div>
  {/if}
  <!-- kept in the layout while hidden: mermaid measures its text, and a `display: none` frame measures nothing -->
  <iframe
    bind:this={frame}
    src={FRAME}
    sandbox="allow-scripts"
    referrerpolicy="no-referrer"
    title={t("diagram.title")}
    class:off={failed !== null || !height}
    style:height="{failed === null ? height : 0}px"
    onload={() => (loaded = true)}
  ></iframe>
</div>

<style>
  .diagram { margin: 0 0 var(--sp-8); }
  .diagram:last-child { margin-bottom: 0; }
  iframe { display: block; width: 100%; border: 0; }
  iframe.off { visibility: hidden; }
  .state { margin-bottom: var(--sp-4); color: var(--muted); font-size: var(--fs-md); }
</style>
