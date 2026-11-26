<script lang="ts">
  // Confirmed runtime events in the existing conversation; no changes to its layout or message bubbles.
  import { t, type Key } from "../lib/i18n.svelte";
  import { stamp, when } from "../lib/time";
  import type { Message } from "../lib/types";

  let { message }: { message: Message } = $props();
  const activity = $derived(message.payload?.activity ?? {});
  const memory = $derived(activity.type === "memory");
  const failed = $derived(["failed", "interrupted"].includes(activity.state));
  const operations: Record<string, Key> = {
    search_knowledge_pages: "activity.memory.search", read_knowledge_page: "activity.memory.read",
    list_knowledge_pages: "activity.memory.list", reflect: "activity.memory.reflect",
    ingest_document: "activity.memory.write", capture_initiative: "activity.memory.initiative",
    sync_status: "activity.memory.sync", diagnose: "activity.memory.diagnose",
  };
  const states: Record<string, Key> = {
    started: "activity.started", returned: "activity.returned", failed: "activity.failed",
    interrupted: "activity.interrupted", selected: "sys.session.instructions_selected",
    loaded: "sys.session.instructions_loaded",
  };
  const validName = (name: unknown): name is string => typeof name === "string" && /^[a-z][a-z0-9-]{0,31}$/.test(name);
  const version = (value: unknown) => Number.isSafeInteger(value) && Number(value) > 0 ? ` v${value}` : "";
  const skills = $derived(Object.entries(activity.skills ?? {}).filter(([name]) => validName(name)).map(
    ([name, value]) => name + version((value as { version?: number })?.version),
  ));
  const heading = $derived(memory ? "Hindsight" : t(states[activity.state] ?? "activity.instructions"));
</script>

<section class="activity" class:memory class:failed aria-label={heading}>
  <div class="head">
    <span class="mark" aria-hidden="true">{memory ? "🧠" : "▤"}</span>
    <strong>{heading}</strong>
    {#if message.payload?.params?.agent}<span class="actor">{message.payload.params.agent}</span>{/if}
    <time datetime={message.created} title={when(message.created)}>{stamp(message.created)}</time>
  </div>
  {#if memory}
    <div>{t(operations[activity.operation] ?? "sys.session.memory")} · {t(states[activity.state] ?? "activity.unknown")}</div>
    {#if activity.state === "returned"}<details><summary>{t("activity.details")}</summary><div class="limit">{t(activity.mode === "write" ? "activity.memory.write_limit" : "activity.memory.read_limit")}</div></details>{/if}
  {:else}
    <div class="items">
      <span>{t("activity.spec")}: <b>{validName(activity.spec) ? activity.spec + version(activity.version) : t("activity.unknown")}</b></span>
      <span>{t("activity.skills")}: <b>{skills.join(", ") || t("activity.none")}</b></span>
    </div>
    {#if activity.state === "loaded"}<details><summary>{t("activity.details")}</summary><div class="limit">{t("activity.instructions_limit")}</div></details>{/if}
  {/if}
</section>

<style>
  .activity { padding: var(--sp-8) var(--sp-12); border: 1px solid var(--line-soft); border-left: 3px solid var(--accent); border-radius: var(--r-sm); background: var(--surface); color: var(--text); font-size: var(--fs-md); overflow-wrap: anywhere; }
  .activity.memory { border-left-color: var(--info); }
  .activity.failed { border-left-color: var(--warn); }
  .head { display: flex; gap: var(--sp-8); align-items: baseline; flex-wrap: wrap; margin-bottom: var(--sp-4); }
  strong, b { font-weight: 500; }
  .mark, .actor { color: var(--muted); }
  time { margin-left: auto; color: var(--muted); font-size: var(--fs-sm); }
  .items { display: flex; gap: var(--sp-4) var(--sp-16); flex-wrap: wrap; }
  .limit { margin-top: var(--sp-4); color: var(--muted); font-size: var(--fs-sm); }
  summary { color: var(--muted); font-size: var(--fs-sm); cursor: pointer; margin-top: var(--sp-4); }
</style>
