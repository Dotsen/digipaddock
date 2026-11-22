<script lang="ts">
  // One compact line in the coordinator's chat until this device gets its replies when the chat is closed.
  // The browser's permission is asked only after "Enable"; "Later" hides it for the session; the result is said in
  // words (on, blocked by the browser with what to do, not supported, needs the installed app, subscription lost).
  import { onMount } from "svelte";
  import { errorText, t } from "../lib/i18n.svelte";
  import { pushDetails, subscribePush } from "../lib/push";
  import { bannerKey, bannerShown, type PushState } from "../lib/pushview";
  import { session } from "../lib/session.svelte";
  import { toast } from "../lib/toast.svelte";

  let { name }: { name: string } = $props();

  const LATER = "kd.push.later";
  let pushState = $state<PushState | null>(null);
  let later = $state(typeof sessionStorage !== "undefined" && sessionStorage.getItem(LATER) === "1");
  let busy = $state(false);

  onMount(() => {
    pushDetails().then(
      (found) => (pushState = found.state),
      () => (pushState = "unsupported"),
    );
  });

  async function enable() {
    if (!session.me) return;
    busy = true;
    try {
      pushState = await subscribePush(session.me.push_public_key);
      if (pushState === "on") toast(t("push.banner.done"), "info");
    } catch (exc) {
      toast(t("push.banner.failed", { error: errorText(exc) }), "error");
    } finally {
      busy = false;
    }
  }

  function postpone() {
    later = true;
    try {
      sessionStorage.setItem(LATER, "1");
    } catch {
      // private mode: it stays hidden on this page
    }
  }
</script>

{#if pushState && pushState !== "on" && bannerShown(pushState, later)}
  <div class="pushbar" role="region" aria-label={t("push.banner.region")}>
    <span class="text">{t(bannerKey(pushState), { name })}</span>
    {#if pushState === "off" || pushState === "lost"}
      <button type="button" class="btn small" disabled={busy} onclick={enable}>{t("push.banner.enable")}</button>
    {/if}
    <button type="button" class="btn small ghost" onclick={postpone}>{t("push.banner.later")}</button>
  </div>
{/if}

<style>
  .pushbar { flex: none; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 8px 10px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface); font-size: 13px; }
  .text { flex: 1; min-width: 180px; color: var(--muted); }
</style>
