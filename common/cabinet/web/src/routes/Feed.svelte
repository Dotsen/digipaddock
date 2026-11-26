<script lang="ts">
  // The feed: journal events the person may see, newest first, with the morning digest among them. Project
  // and kind filters live in the URL; scrolling to the end reads older pages; live events come in on top.
  import { onMount, untrack } from "svelte";
  import Freshness from "../components/Freshness.svelte";
  import { coalesce } from "../lib/coalesce";
  import { actorName, describeEvent, incidentHref, incidentOf } from "../lib/events";
  import { FEED_GROUPS } from "../lib/feed";
  import { errorText, t } from "../lib/i18n.svelte";
  import { live, onLiveEvent, watchAll } from "../lib/live.svelte";
  import { navigate, router } from "../lib/router.svelte";
  import { feed } from "../lib/stores/feed.svelte";
  import { projects } from "../lib/stores/projects.svelte";
  import { synced } from "../lib/synced.svelte";
  import { age, when } from "../lib/time";
  import { toast } from "../lib/toast.svelte";

  const query = $derived(router.current.query);
  const project = $derived(query.get("project") ?? "");
  const group = $derived(query.get("kind") ?? "");
  const groups = Object.keys(FEED_GROUPS) as (keyof typeof FEED_GROUPS)[];

  let sentinel = $state<HTMLElement | null>(null);
  let filter = { project: "", group: "" }; // what the next read is for (not reactive: the effect sets it)

  function setParam(name: string, value: string) {
    const next = new URLSearchParams(query);
    if (value) next.set(name, value);
    else next.delete(name);
    const text = next.toString();
    navigate(text ? `/feed?${text}` : "/feed", { replace: true });
  }

  const failed = (exc: unknown) => toast(errorText(exc), "error");

  // the first page of the filter, then what is newer (a live event, a resync of the app, «retry»); a failed
  // read is retried and the page says so
  const data = synced(() => feed.sync(filter));
  const refresh = coalesce(() => void data.run());

  $effect(() => {
    if (router.current.name !== "feed") return;
    filter = { project, group };
    untrack(() => void data.run());
  });

  $effect(() => {
    if (live.connected) return;
    const tick = setInterval(() => void data.run(), 60_000);
    return () => clearInterval(tick);
  });

  $effect(() => {
    if (!sentinel || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) feed.more().catch(failed);
    });
    observer.observe(sentinel);
    return () => observer.disconnect();
  });

  onMount(() => {
    projects.ensure().catch(failed);
    const release = watchAll();
    const stop = onLiveEvent(refresh);
    return () => {
      refresh.cancel();
      stop();
      release();
      data.dispose();
    };
  });
</script>

<div class="topbar"><h1>{t("nav.feed")}</h1><Freshness {data} /></div>

<div class="filters" role="toolbar">
  <button type="button" class="chip" class:on={!project} onclick={() => setParam("project", "")}>{t("board.all_projects")}</button>
  {#each projects.list as p (p.id)}
    <button type="button" class="chip" class:on={project === p.id} onclick={() => setParam("project", p.id)}>{p.name}</button>
  {/each}
</div>
<label class="select">
  <span>{t("feed.kind")}</span>
  <select value={group} onchange={(e) => setParam("kind", e.currentTarget.value)}>
    <option value="">{t("board.filter.any")}</option>
    {#each groups as g (g)}<option value={g}>{t(`feed.group.${g}`)}</option>{/each}
  </select>
</label>

{#if feed.events.length === 0 && !feed.loading && feed.done}
  <p class="empty">{t("feed.empty")}</p>
{:else}
  <ol class="events">
    {#each feed.events as event (event.seq)}
      {#if event.kind === "digest"}
        <li class="digest">
          <b>{t("push.digest.title")}</b>
          <span>{describeEvent(event)}</span>
          <span class="muted when" title={when(event.ts ?? "")}>{age(event.ts ?? "")}</span>
        </li>
      {:else}
        <li>
          <span class="muted when" title={when(event.ts ?? "")}>{age(event.ts ?? "")}</span>
          <b>{actorName(event.actor)}</b>
          {#if event.card}<a href="/cards/{event.card}">{event.card}</a>{/if}
          {#if incidentOf(event)}
            <a href={incidentHref(incidentOf(event))}>{incidentOf(event)}</a>
          {/if}
          <span class="what">{describeEvent(event)}</span>
        </li>
      {/if}
    {/each}
  </ol>
  {#if !feed.done && !(data.error && !data.updated)}
    <div bind:this={sentinel} class="more">
      <button class="btn" type="button" disabled={feed.loading} onclick={() => feed.more().catch(failed)}>
        {feed.loading ? t("app.loading") : t("feed.more")}
      </button>
    </div>
  {/if}
{/if}

<style>
  .filters { display: flex; gap: var(--sp-6); overflow-x: auto; padding: var(--sp-2) 0 var(--sp-8); }
  .filters .chip { cursor: pointer; }
  .filters .chip.on { color: var(--text); border-color: var(--accent); }
  .select { display: grid; gap: 3px; font-size: var(--fs-sm); color: var(--muted); max-width: 220px; margin-bottom: var(--sp-12); }
  .events { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--sp-2); }
  .events li { display: flex; flex-wrap: wrap; gap: var(--sp-4) var(--sp-8); align-items: baseline; padding: var(--sp-8) var(--sp-2); border-bottom: 1px solid var(--line-soft); font-size: var(--fs-ui); }
  .events b { font-weight: 600; }
  .events a { color: var(--accent); text-decoration: none; }
  .when { font-size: var(--fs-sm); min-width: 52px; }
  .what { flex: 1 1 9em; min-width: 0; overflow-wrap: anywhere; }
  .events li.digest { display: grid; gap: var(--sp-4); background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: var(--sp-12) var(--sp-14); margin: var(--sp-6) 0; }
  .more { display: flex; justify-content: center; padding: var(--sp-14) 0; }
</style>
