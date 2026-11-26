<script lang="ts">
  // A PR's diff as files of numbered lines. While the person writes remarks, each line takes a remark of
  // its own — an added or kept line on the new side, a removed one on the old — gathered in `comments` for the
  // decision to send. A cut diff says so and links the whole one.
  import { COMMENTS_MAX, parseDiff, type DiffLine, type LineComment } from "../lib/diff";
  import { t, type Key } from "../lib/i18n.svelte";

  let {
    text,
    truncated = false,
    url = "",
    commenting = false,
    comments = $bindable([]),
  }: {
    text: string;
    truncated?: boolean;
    url?: string;
    commenting?: boolean;
    comments?: LineComment[];
  } = $props();

  const files = $derived(parseDiff(text));
  const SIGN = { add: "+", del: "−", ctx: " " } as const;

  /** Where a remark on this line points. */
  const at = (line: DiffLine): { side: "new" | "old"; line: number } =>
    line.kind === "del" ? { side: "old", line: line.old ?? 0 } : { side: "new", line: line.new ?? 0 };
  const keyOf = (path: string, line: DiffLine) => `${path}|${at(line).side}|${at(line).line}`;
  const on = (path: string, line: DiffLine) =>
    comments.filter((c) => c.path === path && c.side === at(line).side && c.line === at(line).line);

  let open = $state("");
  let body = $state("");

  function start(path: string, line: DiffLine) {
    open = keyOf(path, line);
    body = "";
  }

  function add(path: string, line: DiffLine) {
    const text = body.trim();
    if (!text) return;
    comments = [...comments, { path, ...at(line), body: text }];
    open = "";
    body = "";
  }

  const remove = (comment: LineComment) => (comments = comments.filter((c) => c !== comment));
</script>

<div class="diff">
  {#each files as file, fi (fi)}
    <section class="file">
      <header>
        <code>{file.oldPath && file.oldPath !== file.path ? `${file.oldPath} → ${file.path}` : file.path}</code>
      </header>
      {#if file.binary}<p class="muted note">{t("decision.pr.binary")}</p>{/if}
      <div class="scroll">
        <table>
          <tbody>
            {#each file.hunks as hunk, hi (hi)}
              <tr class="hunk"><td colspan={commenting ? 4 : 3}>{hunk.header}</td></tr>
              {#each hunk.lines as line, li (li)}
                <tr class={line.kind}>
                  <td class="n">{line.old ?? ""}</td>
                  <td class="n">{line.new ?? ""}</td>
                  <td class="code"><span class="sign" aria-hidden="true">{SIGN[line.kind]}</span>{line.text}</td>
                  {#if commenting}
                    <td class="act">
                      <button
                        type="button"
                        class="plus"
                        disabled={comments.length >= COMMENTS_MAX}
                        aria-label={t("decision.pr.comment_line", { line: at(line).line, side: t(`decision.pr.side.${at(line).side}` as Key) })}
                        onclick={() => start(file.path, line)}>+</button>
                    </td>
                  {/if}
                </tr>
                {#each on(file.path, line) as comment, ci (ci)}
                  <tr class="remark">
                    <td colspan="2"></td>
                    <td colspan={commenting ? 2 : 1}>
                      <span class="body">{comment.body}</span>
                      {#if commenting}
                        <button type="button" class="btn small ghost" aria-label={t("decision.pr.comment_remove")} onclick={() => remove(comment)}>×</button>
                      {/if}
                    </td>
                  </tr>
                {/each}
                {#if commenting && open === keyOf(file.path, line)}
                  <tr class="remark">
                    <td colspan="2"></td>
                    <td colspan="2">
                      <textarea rows="2" aria-label={t("decision.pr.comment_field")} bind:value={body}></textarea>
                      <span class="row">
                        <button type="button" class="btn small primary" disabled={!body.trim()} onclick={() => add(file.path, line)}>
                          {t("decision.pr.comment_add")}
                        </button>
                        <button type="button" class="btn small" onclick={() => (open = "")}>{t("action.cancel")}</button>
                      </span>
                    </td>
                  </tr>
                {/if}
              {/each}
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  {/each}
  {#if truncated}
    <p class="muted note">
      {#if url}<a href={url} target="_blank" rel="noopener noreferrer">{t("decision.pr.cut")}</a>{:else}{t("decision.pr.cut")}{/if}
    </p>
  {/if}
</div>

<style>
  .diff { display: grid; gap: var(--sp-10); min-width: 0; }
  .file { border: 1px solid var(--line); border-radius: var(--r-md); overflow: hidden; min-width: 0; }
  header { padding: var(--sp-6) var(--sp-10); background: var(--surface-2); font-size: var(--fs-sm); overflow-wrap: anywhere; }
  .scroll { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: var(--fs-sm); }
  td { padding: 0 var(--sp-6); vertical-align: top; }
  .n { color: var(--muted); text-align: right; user-select: none; width: 1%; white-space: nowrap; }
  .code { white-space: pre; }
  .sign { display: inline-block; width: 1.2em; color: var(--muted); }
  tr.add { background: color-mix(in srgb, var(--good) 12%, transparent); }
  tr.del { background: color-mix(in srgb, var(--crit) 12%, transparent); }
  .hunk td { color: var(--muted); background: var(--bg); padding: var(--sp-2) var(--sp-6); }
  td.act { width: 1%; }
  .plus { border: 0; background: none; color: var(--accent); font-weight: 700; padding: 0 var(--sp-4); cursor: pointer; }
  .remark td { padding: var(--sp-6); background: var(--surface); font-family: inherit; }
  .remark .body { white-space: pre-wrap; font-family: system-ui, sans-serif; font-size: var(--fs-md); }
  .remark textarea { width: 100%; min-height: 48px; resize: vertical; font-family: system-ui, sans-serif; }
  .row { display: flex; gap: var(--sp-6); margin-top: var(--sp-4); }
  .note { margin: 0; font-size: var(--fs-md); padding: var(--sp-4) var(--sp-10); }
</style>
