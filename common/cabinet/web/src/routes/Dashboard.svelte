<script lang="ts">
  // The overview: the first screen. What waits for the person, the work under way, failures
  // apart from normal waiting, the period's changes (what came since the last look is «new») and the team —
  // all from the cards the person may read. Project and period live in the address; live events refresh it
  // (at most once a second). Only an explicit acknowledgement marks changes seen.
  import { onMount } from "svelte";
  import "../agent-state.css";
  import Freshness from "../components/Freshness.svelte";
  import { statusKey } from "../components/StatusPill.svelte";
  import { apiPost } from "../lib/api";
  import { coalesce } from "../lib/coalesce";
  import { isToolMessage } from "../lib/events";
  import { errorText, t, type Key } from "../lib/i18n.svelte";
  import { onLiveEvent, watchAll } from "../lib/live.svelte";
  import { navigate, router } from "../lib/router.svelte";
  import { reasonText } from "../lib/runReason";
  import { groupHref } from "../lib/incidentGroups";
  import { checkName, incidentHref, shortValue } from "../lib/servers";
  import { session } from "../lib/session.svelte";
  import { overview, PERIODS } from "../lib/stores/overview.svelte";
  import { projects } from "../lib/stores/projects.svelte";
  import { synced } from "../lib/synced.svelte";
  import { age, when } from "../lib/time";
  import type { OverviewChange, OverviewMember, OverviewNeed, OverviewPeriod, OverviewProblem, OverviewWork } from "../lib/types";

  const query = $derived(router.current.query);
  const project = $derived(query.get("project") ?? "");
  const period = $derived((PERIODS as string[]).includes(query.get("period") ?? "") ? (query.get("period") as OverviewPeriod) : "1d");
  const found = $derived(overview.data);

  let openSeen = $state<number | null>(null);
  let marking = $state(false);
  let markError = $state("");
  let gone = false;

  const data = synced(async () => {
    await overview.load({ project, period });
    if (openSeen === null && overview.data) openSeen = overview.data.seen;
  });
  const refresh = coalesce(() => void data.run());

  // Explicit acknowledgement: scrolling panels and background refreshes do not mean the operator read them.
  // A filtered overview cannot acknowledge changes hidden in the other projects.
  async function markSeen() {
    if (!found || project || marking || gone) return;
    const seq = found.watermark;
    marking = true;
    markError = "";
    try {
      await apiPost("/api/dashboard/seen", { seq });
      if (!gone) openSeen = seq;
    } catch (exc) {
      if (!gone) markError = errorText(exc);
    } finally {
      marking = false;
    }
  }

  $effect(() => {
    if (router.current.name !== "dashboard") return;
    void [project, period];
    void data.run();
  });

  onMount(() => {
    projects.ensure().catch(() => undefined);
    const release = watchAll();
    // a turn's tool calls and progress lines change nothing here
    const stop = onLiveEvent((event) => {
      if (!isToolMessage(event) && !(event.kind === "message.added" && event.payload?.kind === "progress")) refresh();
    });
    const tick = setInterval(() => void data.run(), 60_000); // a late start and a stale heartbeat send no event
    return () => {
      gone = true;
      refresh.cancel();
      clearInterval(tick);
      stop();
      release();
      data.dispose();
    };
  });

  function setParam(name: string, value: string) {
    const next = new URLSearchParams(query);
    if (value) next.set(name, value);
    else next.delete(name);
    const text = next.toString();
    navigate(text ? `/?${text}` : "/", { replace: true });
  }

  const isNew = (change: OverviewChange) => (openSeen === null ? change.new : change.seq > openSeen);
  const fresh = $derived(found ? found.changes.filter(isNew).length : 0);

  function needKind(need: OverviewNeed): string {
    return need.kind === "review" ? t("overview.need.review") : t(`decision.type.${need.kind}` as Key);
  }

  function stage(row: OverviewWork): string {
    return row.status === "Inbox" ? t(`overview.stage.${row.stage}` as Key) : t(statusKey(row.status));
  }

  function problem(row: OverviewProblem): string {
    if (row.kind === "error") return reasonText(row.reason);
    if (row.kind === "stuck") return t(`overview.stuck.${row.reason}` as Key, { agent: row.agent });
    if (row.reason === "deps") return t("overview.waiting.deps", { deps: (row.deps ?? []).join(", ") });
    return t(`overview.waiting.${row.reason}` as Key);
  }

  function change(row: OverviewChange): string {
    if (row.kind === "handoff") return t("overview.change.handoff", { to: row.to ?? "" });
    if (row.kind === "brief") return t("overview.change.brief", { version: row.version ?? "" });
    return t(`overview.change.${row.kind}` as Key);
  }

  const NUMBERS = ["working", "waiting_you", "queued", "failed"] as const;
  const numbers = (member: OverviewMember) => NUMBERS.filter((key) => member[key] > 0);
  const active = $derived(found?.work.filter((row) => row.run === "working").length ?? 0);
  const queued = $derived(found?.work.filter((row) => row.run === "queued" || row.run === "delayed").length ?? 0);
  // a card stuck with nobody on it asks for a decision as a failure does; an incident is the servers'
  const failures = $derived(found?.problems.filter((row) => row.kind === "error" || row.kind === "stuck").length ?? 0);
  const waits = $derived(found?.problems.filter((row) => row.kind === "waiting").length ?? 0);
  const questions = $derived(found?.needs.filter((row) => row.kind === "question").length ?? 0);
  const reviews = $derived(found?.needs.filter((row) => row.kind === "review").length ?? 0);
  const needs = $derived([...(found?.needs ?? [])].sort((a, b) => a.since.localeCompare(b.since)));
  const RANK = { incident: -1, error: 0, stuck: 1, waiting: 2 } as const;
  const problems = $derived([...(found?.problems ?? [])].sort((a, b) => RANK[a.kind] - RANK[b.kind]));
  const boardLink = $derived(project ? `/board?project=${encodeURIComponent(project)}` : "/board");
  const feedLink = $derived(project ? `/feed?project=${encodeURIComponent(project)}` : "/feed");
</script>

<div class="dashboard">
  <div class="topbar">
    <h1>{t("nav.dashboard")}</h1>
    <Freshness {data} />
    <span class="spacer"></span>
    <!-- shortcuts the side menu has too; a phone's tab bar has the incidents and the panels link the board -->
    {#if session.can("servers.read")}<a class="btn small ghost shortcut" href="/incidents">{t("nav.incidents")} →</a>{/if}
    <a class="btn small ghost shortcut" href={boardLink}>{t("overview.open_board")} →</a>
  </div>

  <div class="filters" role="toolbar" aria-label={t("overview.filters")}>
    <label class="project-filter">
      <span class="visually-hidden">{t("board.all_projects")}</span>
      <select value={project} onchange={(e) => setParam("project", e.currentTarget.value)}>
        <option value="">{t("board.all_projects")}</option>
        {#each projects.list as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
      </select>
    </label>
    <span class="muted scope">{t("overview.operator_hint")}</span>
  </div>

  {#if !found}
    {#if data.error}<p class="empty" role="alert">{errorText(data.error)}</p>{:else}<p class="empty">{t("app.loading")}</p>{/if}
  {:else}
    <nav class="counts" aria-label={t("overview.summary")}>
      <a href="#needs" class:hot={found.counts.needs > 0} aria-label={`${found.counts.needs} ${t("overview.needs")}`}>
        <span>{t("overview.needs")}</span><b class="num">{found.counts.needs}</b>
        <small>{t("overview.need_breakdown", { questions, reviews, approvals: found.needs.length - questions - reviews })}</small>
      </a>
      <a href="#work" aria-label={`${active} ${t("overview.executing")}`}>
        <span>{t("overview.executing")}</span><b class="num good">{active}</b>
        <small>{t("overview.work_total", { n: found.counts.work })}</small>
      </a>
      <a href="#problems" class:bad={failures > 0} aria-label={`${failures} ${t("overview.errors")}`}>
        <span>{t("overview.errors")}</span><b class="num">{failures}</b>
        <small>{t("overview.wait_total", { n: waits })}</small>
      </a>
      <a href="#work" aria-label={`${queued} ${t("overview.queue")}`}>
        <span>{t("overview.queue")}</span><b class="num">{queued}</b>
        <small>{t("overview.queue_hint")}</small>
      </a>
      {#if found.servers}
        {@const s = found.servers}
        <a class="servers" class:bad={s.crit + s.unreachable > 0} class:warn={s.crit + s.unreachable === 0 && s.warn > 0}
          href="/servers" aria-label={`${t("overview.servers")}: ${t("overview.servers_hint", { ...s })}`}>
          <span>{t("overview.servers")}</span><b class="num">{s.crit + s.unreachable}</b>
          <small>{t("overview.servers_hint", { ...s })}</small>
        </a>
      {/if}
    </nav>

    <div class="panels">
      <section id="needs" class="attention" aria-labelledby="needs-title">
        <header><h2 id="needs-title">{t("overview.needs")} <span class="n num">{found.needs.length}</span></h2><a href="/inbox">{t("overview.open_inbox")} →</a></header>
        <!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard access to the scrollable region) -->
        <div class="panel-body" tabindex="0" role="region" aria-labelledby="needs-title">
          {#each needs as need (need.kind + need.card + need.decision)}
            <div class="entry">
              <div class="details"><span class="chip">{needKind(need)}</span><span class="muted meta" title={when(need.since)}>{t("overview.waits", { who: need.waiting, age: age(need.since) })}</span></div>
              <a class="title" href={need.link}><span class="id">{need.card}</span> {need.title}</a>
            </div>
          {:else}<p class="empty">{t("overview.empty.needs")}</p>{/each}
        </div>
      </section>

      <section id="work" aria-labelledby="work-title">
        <header><h2 id="work-title">{t("overview.work")} <span class="n num">{found.work.length}</span></h2><a href={boardLink}>{t("overview.open_board")} →</a></header>
        <!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard access to the scrollable region) -->
        <div class="panel-body" tabindex="0" role="region" aria-labelledby="work-title">
          {#each found.work as row (row.card)}
            <div class="entry">
              <div class="details"><span class="agent">{row.assignee || "—"}</span><span class="muted meta">{stage(row)}</span><span class="run {row.run}">{t(`overview.run.${row.run}`)}</span></div>
              <a class="title" href="/cards/{row.card}"><span class="id">{row.card}</span> {row.title}</a>
              {#if row.last_step}<span class="step muted" title={when(row.last_step.at)}><span>{row.last_step.text}</span> · {age(row.last_step.at)}</span>{/if}
            </div>
          {:else}<p class="empty">{t("overview.empty.work")}</p>{/each}
        </div>
      </section>

      <section id="problems" aria-labelledby="problems-title">
        <header><h2 id="problems-title">{t("overview.problems")} <span class="n num">{found.problems.length}</span></h2><span class="muted meta">{t("overview.runs_only")}</span></header>
        <!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard access to the scrollable region) -->
        <div class="panel-body" tabindex="0" role="region" aria-labelledby="problems-title">
          {#each problems as row (row.kind === "incident" ? row.incident : row.kind + row.reason + row.card)}
            {#if row.kind === "incident"}
              <!-- an open crit nobody acknowledged or muted -->
              <div class="entry">
                {#if row.group && row.members}<!-- one tick's crit on several servers — one row -->
                  <div class="details"><span class="chip crit">{t("overview.kind.incident")}</span><span class="muted meta" title={when(row.since)}>{t("mon.group.servers", { n: row.servers?.length ?? 0 })} · {age(row.since)}</span></div>
                  <a class="title" href={groupHref(row.group.id)}><span class="id">{row.incident}</span> {checkName(row.check, row.group.subjects > 1 ? "" : row.subject)} · {t("mon.group.count", { n: row.members.length })}</a>
                {:else}
                <div class="details"><span class="chip crit">{t("overview.kind.incident")}</span><span class="muted meta" title={when(row.since)}>{row.server} · {age(row.since)}</span></div>
                <a class="title" href={incidentHref(row.incident)}><span class="id">{row.incident}</span> {checkName(row.check, row.subject)}: {shortValue(row.value)}</a>
                {/if}
              </div>
            {:else}
              <div class="entry">
                <div class="details"><span class="chip {row.kind === 'waiting' ? 'warn' : 'crit'}">{t(`overview.kind.${row.kind}`)}</span><span class="muted meta">{row.agent} · {age(row.since)}</span></div>
                <a class="title" href="/cards/{row.card}"><span class="id">{row.card}</span> {row.title}</a>
                <span class="meta reason" title={row.reason}>{problem(row)}</span>
              </div>
            {/if}
          {:else}<p class="empty">{t("overview.empty.problems")}</p>{/each}
        </div>
      </section>

      <section id="changes" aria-labelledby="changes-title">
        <header><h2 id="changes-title">{t("overview.changes")} {#if fresh}<span class="chip info">{t("overview.count.changes", { n: fresh })}</span>{/if}</h2><a href={feedLink}>{t("overview.open_feed")} →</a></header>
        <div class="change-tools">
          <div class="periods" role="group" aria-label={t("overview.period")}>
            {#each PERIODS as p (p)}<button type="button" class:on={period === p} aria-pressed={period === p} onclick={() => setParam("period", p === "1d" ? "" : p)}>{t(`overview.period.${p}`)}</button>{/each}
          </div>
          {#if !project && fresh}<button class="ack" type="button" disabled={marking} onclick={() => void markSeen()}>{t("overview.mark_seen")}</button>{/if}
        </div>
        {#if markError}<p class="error-text" role="alert">{markError}</p>{/if}
        <!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard access to the scrollable region) -->
        <div class="panel-body" tabindex="0" role="region" aria-labelledby="changes-title">
          {#each found.changes as row (row.seq)}
            <div class="entry" class:fresh={isNew(row)}>
              <div class="details"><span class="what">{change(row)}</span><span class="muted meta" title={when(row.at)}>{row.actor_name} · {age(row.at)}</span>{#if isNew(row)}<span class="chip info">{t("overview.new")}</span>{/if}</div>
              <a class="title" href="/cards/{row.card}"><span class="id">{row.card}</span> {row.title}</a>
            </div>
          {:else}<p class="empty">{t("overview.empty.changes")}</p>{/each}
        </div>
      </section>
    </div>

    <section id="team" aria-labelledby="team-title">
      <header><h2 id="team-title">{t("overview.team")}</h2><a href="/agents">{t("overview.open_agents")} →</a></header>
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard access to the scrollable region) -->
      <div class="crew" tabindex="0" role="region" aria-labelledby="team-title">
        {#each found.team as member (member.name)}
          <a class="member agent-state {member.state}" href="/agents/{member.name}">
            <div class="details"><b>@{member.name}</b><span class="chip state"><span class="dot" aria-hidden="true"></span>{t(`agents.state.${member.state}`)}</span></div>
            <div class="facts">{#each numbers(member) as key (key)}<span>{t(`overview.team.${key}`, { n: member[key] })}</span>{/each}</div>
          </a>
        {:else}<p class="empty">{t("overview.empty.team")}</p>{/each}
      </div>
    </section>
  {/if}
</div>

<style>
  .dashboard { min-width: 0; }
  .topbar { position: static; }
  .filters { display: flex; align-items: center; gap: 16px; margin-bottom: 12px; }
  .project-filter { width: 210px; flex: none; }
  select { padding: 6px 10px; font-size: 13px; }
  .scope { font-size: 13px; }
  .counts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
  .counts a { display: grid; align-content: start; gap: 2px; padding: 12px 14px; border-radius: var(--radius); background: var(--surface); border: 1px solid var(--line); color: var(--muted); text-decoration: none; font-size: 13px; min-width: 0; }
  .counts b { font-size: 30px; line-height: 1.2; font-weight: 650; color: var(--text); }
  .counts small { font-size: 11px; overflow-wrap: anywhere; }
  .counts a.hot { border-color: var(--accent); }
  .counts a.hot b { color: var(--accent); }
  .counts a.bad b { color: var(--crit); }
  .counts b.good { color: var(--good); }
  .counts .servers { grid-column: 1 / -1; }
  .counts .servers.bad { border-color: var(--crit); }
  .counts .servers.warn { border-color: var(--warn); }
  .counts .servers.warn b { color: var(--warn); }
  .panels { display: grid; gap: 14px; }
  section { min-width: 0; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); display: flex; flex-direction: column; overflow: hidden; scroll-margin-top: 16px; }
  section.attention { border-top: 2px solid var(--accent); }
  header { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 12px 14px; flex: none; border-bottom: 1px solid var(--line-soft); }
  h2 { margin: 0; font-size: 14px; font-weight: 600; }
  h2 .n { color: var(--muted); font-weight: 400; margin-left: 5px; }
  h2 .chip { margin-left: 5px; font-size: 10px; }
  header a { font-size: 12px; white-space: nowrap; text-decoration: none; }
  .panel-body { min-height: 0; overflow-y: auto; overscroll-behavior-y: contain; scrollbar-width: thin; max-height: 320px; flex: 1; }
  .entry { display: flex; flex-direction: column; gap: 4px; padding: 10px 14px; border-bottom: 1px solid var(--line-soft); min-width: 0; }
  .entry:last-child { border-bottom: 0; }
  .entry.fresh { border-left: 2px solid var(--info); background: color-mix(in srgb, var(--info-bg) 25%, transparent); }
  .details { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 8px; min-width: 0; }
  .title { color: inherit; text-decoration: none; font-size: 14px; overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .title:hover { color: var(--accent); }
  .id { color: var(--muted); font-size: 11px; margin-right: 4px; }
  .meta, .step, .what, .run, .agent { font-size: 12px; overflow-wrap: anywhere; }
  .step { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  .reason { color: var(--warn); }
  .run { margin-left: auto; }
  .run.failed, .run.delayed, .run.stale { color: var(--warn); }
  .run.working { color: var(--good); }
  .chip { font-size: 11px; white-space: normal; }
  .chip.crit { color: var(--crit); background: var(--crit-bg); }
  .chip.warn { color: var(--warn); background: var(--warn-bg); }
  .chip.info { color: var(--info); background: var(--info-bg); }
  .change-tools { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 14px; flex: none; border-bottom: 1px solid var(--line-soft); }
  .periods { display: flex; gap: 4px; }
  .periods button, .ack { background: none; border: 0; color: var(--muted); padding: 4px; font-size: 11px; }
  .periods button.on { color: var(--text); background: var(--surface-3); border-radius: 4px; }
  .ack { color: var(--accent); }
  .empty { margin: 0; padding: 22px 14px; font-size: 13px; }
  .error-text { margin: 0; padding: 4px 14px; }
  #team { margin-top: 14px; }
  .crew { display: flex; gap: 8px; padding: 8px; overflow: auto; scrollbar-width: thin; }
  .member { flex: 1 0 210px; min-width: 0; padding: 8px 12px; border: 2px var(--state-border-style) var(--state-color); border-radius: 8px; color: inherit; text-decoration: none; }
  .member:hover { background: var(--surface-2); }
  .member b { font-size: 13px; }
  .facts { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 4px; font-size: 11px; color: var(--muted); }
  @media (min-width: 900px) {
    .dashboard { display: flex; flex-direction: column; flex: 1; min-height: 0; }
    .topbar, .filters, .counts, #team { flex: none; }
    .counts { grid-template-columns: repeat(4, minmax(0, 1fr)) minmax(180px, 1.25fr); }
    .counts .servers { grid-column: auto; }
    .panels { flex: 1; min-height: 0; grid-template-columns: repeat(2, minmax(0, 1fr)); grid-template-rows: repeat(2, minmax(0, 1fr)); }
    .panel-body { max-height: none; }
    .crew { max-height: 92px; }
  }
  .topbar :global(.fresh) { white-space: nowrap; }
  @media (max-width: 599px) {
    .shortcut { display: none; }
    .counts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .scope { display: none; }
    .project-filter { width: 100%; }
  }
</style>
