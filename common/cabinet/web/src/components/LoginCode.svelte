<script lang="ts">
  // A sign-in code just issued: the code once, a copy button and the time it still works; once it
  // has passed the code is struck through. The same panel serves one's own new device (Settings) and a
  // person the owner gives a code to (people and roles).
  import { onDestroy } from "svelte";
  import { t } from "../lib/i18n.svelte";
  import { countdown } from "../lib/time";
  import { toast } from "../lib/toast.svelte";

  let {
    title,
    code,
    expires = "",
    hint = t("users.code_hint"),
    onclose,
  }: { title: string; code: string; expires?: string; hint?: string; onclose: () => void } = $props();

  let now = $state(Date.now());
  const timer = setInterval(() => (now = Date.now()), 1000);
  onDestroy(() => clearInterval(timer));
  const left = $derived(countdown(expires, now));
  const over = $derived(!!expires && !left);

  async function copy() {
    try {
      await navigator.clipboard.writeText(code);
      toast(t("users.copied"), "info");
    } catch {
      toast(t("users.copy_failed"), "error");
    }
  }
</script>

<section class="panel stack code" role="status">
  <b>{title}</b>
  <code class="value" class:over>{code}</code>
  {#if over}
    <span class="muted">{t("code.expired")}</span>
  {:else if left}
    <span class="muted">{t("code.left", { left })}</span>
  {/if}
  <span class="muted">{hint}</span>
  <div class="row">
    {#if !over}<button class="btn small" type="button" onclick={copy}>{t("users.copy")}</button>{/if}
    <button class="btn small primary" type="button" onclick={onclose}>{t("setup.finish")}</button>
  </div>
</section>

<style>
  .code { border-color: var(--accent); }
  .value { font-family: var(--font-mono); font-size: 22px; letter-spacing: .06em; user-select: all; }
  .value.over { text-decoration: line-through; opacity: .5; user-select: none; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
</style>
