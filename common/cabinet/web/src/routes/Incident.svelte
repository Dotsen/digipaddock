<script lang="ts">
  import { onMount } from "svelte";
  import ActionMenu from "../components/ActionMenu.svelte";
  import BackButton from "../components/BackButton.svelte";
  import Freshness from "../components/Freshness.svelte";
  import LinkedCards from "../components/LinkedCards.svelte";
  import MonitorNav from "../components/MonitorNav.svelte";
  import Markdown from "../components/Markdown.svelte";
  import Sheet from "../components/Sheet.svelte";
  import SignDialog from "../components/SignDialog.svelte";
  import TrustList from "../components/TrustList.svelte";
  import { apiGet, apiPost, ApiError } from "../lib/api";
  import { NoPasskey, approveDecisions, withPasskey } from "../lib/decide";
  import { actorName, describeEvent } from "../lib/events";
  import { errorText, t, type Key } from "../lib/i18n.svelte";
  import { onLiveEvent, watchAll } from "../lib/live.svelte";
  import { cardFieldPattern, changeText, checkTitle, explanation, incidentActions, incidentAsk, levelText, quietState, resolutionText, thresholdLine, type IncidentAction } from "../lib/monitoring";
  import { MUTES, serverHref, touchesServers } from "../lib/servers";
  import { session } from "../lib/session.svelte";
  import { projects } from "../lib/stores/projects.svelte";
  import { synced } from "../lib/synced.svelte";
  import { age, exactWhen, when } from "../lib/time";
  import { toast } from "../lib/toast.svelte";
  import { propose } from "../lib/trust";
  import type { CheckRequest, DecisionItem, IncidentPage } from "../lib/types";
  import { isCancelled } from "../lib/webauthn";

  // An action with a form opens its sheet from below
  type SheetKind = "accept" | "revoke" | "quiet" | "handoff" | "link";
  // the "Actions" rows' line icons, in the navigation's style
  const ICONS: Record<IncidentAction, string> = {
    accept: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/>',
    revoke: '<circle cx="8" cy="15" r="4"/><path d="M10.8 12.2L20 3"/><path d="M16 7l3 3"/><path d="M3 3l18 18"/>',
    quiet: '<path d="M8.7 4.4A6 6 0 0 1 18 9.5V14l1.5 2.5"/><path d="M6.1 7.6A6 6 0 0 0 6 9.5V14l-2 3h12"/><path d="M10 20h4"/><path d="M3 3l18 18"/>',
    unquiet: '<path d="M6 9.5a6 6 0 0 1 12 0V14l2 3H4l2-3z"/><path d="M10 20h4"/>',
    recheck: '<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v5h-5"/>',
    link: '<path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1"/><path d="M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1"/>',
  };

  let { id }: { id: string } = $props();
  let page = $state<IncidentPage | null>(null);
  let missing = $state(false);
  let busy = $state(false);
  let sheet = $state<SheetKind | null>(null);
  let handoffTitle = $state("");
  let project = $state("");
  let comment = $state("");
  let label = $state("");
  let acceptError = $state("");
  let signing = $state<DecisionItem | null>(null);
  let now = $state(Date.now());
  let existingId = $state("");
  let transfer = $state(false);
  let existing = $state<{id:string; title:string; revision:number; assignee:string; project:string} | null>(null);
  const data = synced(async () => {
    try { page = await apiGet<IncidentPage>(`/api/incidents/${encodeURIComponent(id)}`); missing = false; }
    catch (error) { if (error instanceof ApiError && error.status === 404) missing = true; else throw error; }
  });
  const canAct = $derived(session.can("incidents.ack"));
  const availableProjects = $derived(projects.list.filter(p => session.can("cards.create", p.id)));
  const i = $derived(page?.incident);
  const latest = $derived(i?.current ?? (i ? {...i, at:i.last_seen} : null));
  const canTrust = $derived(!!page?.acceptable && i?.status !== "resolved" && session.can("decide.monitor_trust"));
  // a login by a key whose access is already gone closes as "access revoked" — never into the baseline
  const canRevoke = $derived(!!page?.revocable && i?.status !== "resolved" && session.can("decide.monitor_trust"));
  const weakLogin = $derived(i?.check === "logins" && /@(password|key \?)$/.test(i.subject));
  const opening = $derived(page?.timeline.find(e => e.kind === "incident.opened"));
  const report = $derived(i?.investigation?.report);
  // what the page asks of the owner, its one "Do not remind", a file's content in words
  const ask = $derived(page ? incidentAsk(page) : null);
  const quiet = $derived(i ? quietState(i, now) : null);
  const content = $derived(!!i && i.check.startsWith("sec:"));
  const changes = $derived(i?.changes ?? {count:0, last_at:""});
  const acceptTitle = $derived(t(weakLogin ? "mon.accept_login_event" : "trust.button.accept"));
  const actions = $derived(ask ? incidentActions(ask, {canTrust, canRevoke, canAct, quiet:!!quiet}).map(action => ({
    id: action, icon: ICONS[action], text: actionText(action), ...(action === "accept" ? {tone: "good" as const} : {}),
  })) : []);
  onMount(() => {
    void data.run();
    const release = watchAll();
    const stop = onLiveEvent(e => { if (touchesServers(e) || e.kind.startsWith("card.") || e.kind.startsWith("command.")) void data.run(); });
    const tick = setInterval(() => { now = Date.now(); void data.run(); }, 30_000);
    return () => { clearInterval(tick); stop(); release(); data.dispose(); };
  });

  function actionText(action: IncidentAction): string {
    if (action === "accept") return acceptTitle;
    if (action === "revoke") return t("trust.button.revoke");
    if (action === "quiet") return t("mon.quiet.button");
    if (action === "unquiet") return t("mon.quiet.on");
    if (action === "recheck") return t("mon.recheck");
    return t("mon.link_existing");
  }
  function onAction(action: string) {
    if (action === "recheck" && i) void recheck(i.server);
    else if (action === "unquiet") void act("unmute");
    else if (action === "link") { existing = null; sheet = "link"; }
    else { acceptError = ""; sheet = action as SheetKind; }
  }
  async function act(action: string, body: unknown = {}) {
    busy = true;
    try { await apiPost(`/api/incidents/${encodeURIComponent(id)}/${action}`, body); sheet = null; await data.run(); }
    catch (error) { toast(error instanceof ApiError ? `${errorText(error)}: ${error.message}` : errorText(error), "error"); }
    finally { busy = false; }
  }
  function startHandoff(title: string, why = "") {
    project = availableProjects.find(p => p.route === "@sysadmin")?.id ?? availableProjects[0]?.id ?? "";
    if (why && !comment) comment = why;
    handoffTitle = title;
    sheet = "handoff";
  }
  // The sheet says what is accepted; its button signs with the passkey at once (no second dialog). Without a passkey
  // here (a TOTP code) or with a decision changed since the page was read, the signing dialog takes over.
  async function accept(kind: "accept" | "revoke" = "accept") {
    busy = true;
    acceptError = "";
    try {
      const named = kind === "accept" && label.trim() ? {label:label.trim()} : {};
      const item = await propose({kind, incident:id, ...named});
      try { await approveDecisions([item], withPasskey); sheet = null; await data.run(); }
      catch (exc) {
        if (isCancelled(exc)) acceptError = t("error.passkey_cancelled");
        else if (exc instanceof NoPasskey || (exc instanceof ApiError && exc.status === 409)) { sheet = null; signing = item; }
        else acceptError = errorText(exc);
      }
    }
    catch (error) { acceptError = errorText(error); } finally { busy = false; }
  }
  function signed() { signing = null; void data.run(); }
  async function previewLink() {
    busy = true;
    try { existing = await apiGet(`/api/cards/${encodeURIComponent(existingId.trim().toUpperCase())}`); }
    catch (error) { toast(errorText(error), "error"); } finally {busy=false;}
  }
  async function recheck(server: string) {
    busy = true;
    try { await apiPost(`/api/servers/${encodeURIComponent(server)}/check`, {}); await data.run(); }
    catch (error) { toast(errorText(error), "error"); } finally { busy = false; }
  }
  const recheckText = (r: CheckRequest) =>
    t(`mon.recheck.${r.state}` as Key, {at: when(r.finished || r.started || r.created), detail: r.detail || "—"});
</script>

<div class="monitor-page">
  <div class="topbar"><BackButton /><h1>{id}</h1><span class="spacer"></span><Freshness {data} /></div>
  <MonitorNav active="incidents" />
  {#if missing}<p class="empty">{t("incident.notfound")}</p>
  {:else if !page || !i || !latest}<p class="empty">{data.error ? errorText(data.error) : t("app.loading")}</p>
  {:else}
    <section class="monitor-summary">
      <h2 class="monitor-title">{checkTitle(i.check, i.subject)}</h2>
      <div class="monitor-row-head">
        <span class="chip monitor-badge" class:active={i.status !== "resolved"} class:good={i.status === "resolved"}>{i.status === "resolved" ? t("mon.closed") : t("mon.active")}</span>
        <span class="chip monitor-badge {i.level}">{levelText(i.level)}</span>
      </div>
      {#if i.status === "resolved"}<p>{resolutionText(i)}</p>
      {:else if ask}
        <!-- the question and its 1–2 buttons first, the rest under "Actions" -->
        <section class="incident-ask" aria-label={t("mon.ask.title")}>
          <h3>{t("mon.ask.title")}</h3>
          {#if ask.step === "accepting"}<p role="status">{t(ask.revoke ? "mon.ask.revoking" : "mon.ask.accepting", {at:when(ask.at)})}</p>
          {:else if ask.step === "card"}<p>{t("mon.ask.card", {card:ask.card.id, assignee:ask.card.assignee})}</p>
          {:else if ask.step === "confirm_change"}
            {#if ask.failed !== undefined}<p class="error-text" role="alert">{t(ask.revokeFailed ? "mon.ask.revoke_failed" : "mon.ask.accept_failed", {detail:ask.failed || "—"})}</p>{/if}
            <p><b>{t(ask.login ? "mon.ask.confirm_login" : "mon.ask.confirm_change")}</b></p>
            <p class="monitor-note">{t(ask.login ? "mon.ask.confirm_login_hint" : "mon.ask.confirm_change_hint")}</p>
            {#if canRevoke}<p class="monitor-note">{t("mon.ask.confirm_login_revoke_hint")}</p>{/if}
          {:else if ask.step === "review_report"}<p>{t("mon.ask.review_report")}</p>
          {:else if ask.step === "investigating"}<p>{t("mon.ask.investigating")}</p>
          {:else}<p>{t("mon.ask.wait_recovery")}</p>{/if}
          <div class="monitor-actions">
            {#if ask.step === "card"}<a class="btn primary" href="/cards/{ask.card.id}">{t("mon.open_work")} {ask.card.id}</a>
            {:else if ask.step === "confirm_change"}
              {#if canTrust}<button class="btn primary" disabled={busy} aria-haspopup="dialog" onclick={() => onAction("accept")}>{t("mon.ask.yes")}</button>{/if}
              {#if canRevoke}<button class="btn" disabled={busy} aria-haspopup="dialog" onclick={() => onAction("revoke")}>{t("trust.button.revoke")}</button>{/if}
              {#if availableProjects.length}<button class="btn warn" disabled={busy} aria-haspopup="dialog" onclick={() => startHandoff(t("mon.handoff"), t("mon.ask.not_mine"))}>{t("mon.ask.no")}</button>{/if}
            {:else if ask.step !== "accepting" && availableProjects.length}
              {@const followup = ask.step === "review_report"}
              <button class="btn" class:primary={followup} disabled={busy} aria-haspopup="dialog"
                onclick={() => startHandoff(t(followup ? "mon.create_followup" : "mon.handoff"))}>{t(followup ? "mon.create_followup" : "mon.handoff")}</button>
            {/if}
            {#if actions.length}<span class="incident-more"><ActionMenu label={t("mon.actions")} items={actions} disabled={busy} onpick={onAction} /></span>{/if}
          </div>
          {#if quiet}<p class="monitor-note" role="status">{quiet.until === "closed" ? t("mon.quiet.until_closed") : t("mon.quiet.until", {at:when(quiet.until)})}</p>{/if}
          {#if i.recheck}<p class="monitor-note" role="status">{recheckText(i.recheck)}</p>{/if}
        </section>
      {/if}
    </section>
    <section class="monitor-summary">
      <dl class="monitor-facts">
        <div><dt>{t("incident.server")}:</dt><dd><a href={serverHref(i.server)}>{i.server}</a></dd></div>
        <div><dt>{t("mon.started")}</dt><dd><time datetime={i.opened_at}>{exactWhen(i.opened_at)}</time></dd></div>
        <div><dt>{t("mon.last_confirmed")}</dt><dd>
          {#if i.confirmation_at || i.status !== "resolved"}<time datetime={i.confirmation_at || i.last_seen}>{exactWhen(i.confirmation_at || i.last_seen)}</time>
          {:else}{t("mon.unknown")}{/if}
        </dd></div>
        <div><dt>{t("mon.investigation")}</dt><dd>{t(`mon.analysis.${i.investigation?.state ?? "waiting"}`)}</dd></div>
        {#if ask?.step === "card"}<div><dt>{t("mon.assignee")}</dt><dd>{ask.card.assignee}</dd></div>{/if}
      </dl>
      <section class="monitor-analysis" aria-label={t("mon.investigation")}>
        <h3>{t("mon.investigation")}</h3>
        {#if report}
          <p class="monitor-note">{t("mon.report_by", {at:exactWhen(report.created)})}</p>
          <Markdown text={report.body} />
          {#if session.can("agents.chat")}<a class="monitor-note" href="/agents/sysadmin?tab=monitor">{t("mon.report_source")}</a>{/if}
        {:else}<p>{t("mon.analysis_pending")}</p>{/if}
        {#if i.investigation?.detail}<p class="error-text">{i.investigation.detail}</p>{/if}
      </section>
      <details class="monitor-details" open={!report}><summary>{t("mon.monitor_observation")}</summary>
      {#if content}{#if latest.text || i.text}<p><b>{latest.text || i.text}</b></p>{/if}<p>{changeText(i, changes)}</p>
      {:else}<p><b>{latest.value || i.value || "—"}</b></p>{#if latest.text}<p>{latest.text}</p>{/if}{/if}
      {#if !report}<p>{explanation({...i, value:latest.value})}</p>{/if}
      {#if i.current && thresholdLine(i.current)}<p class="monitor-note">{thresholdLine(i.current)}</p>{/if}
      <p class="monitor-note">{t("mon.measured", {age:age(latest.at ?? i.last_seen)})} · {when(latest.at ?? i.last_seen)}</p>
      </details>
      {#if i.status !== "resolved" && i.freshness !== "fresh"}<p class="monitor-warning">{t("mon.data_attention")}</p>{/if}
      {#if i.delivery}<p role="status">{t(`mon.delivery.${i.delivery.state}` as Key)}{#if i.delivery.detail}: {i.delivery.detail}{/if}</p>{/if}
      {#if i.delivery && ["failed", "cancelled"].includes(i.delivery.state) && ask?.step === "card" && session.can("cards.edit", ask.card.project)}
        <button class="btn" disabled={busy} onclick={() => act("retry")}>{t("mon.retry_delivery")}</button>
      {/if}
    </section>
    <section class="monitor-section"><h2>{t("incident.cards")}</h2><LinkedCards cards={page.cards} /></section>
    {#if page.trust?.length}<section class="monitor-section"><h2>{t("mon.trust_history")}</h2><TrustList decisions={page.trust} /></section>{/if}
    <details class="monitor-details"><summary>{t("mon.technical_details")}</summary>
      <p>{t("incident.check")}: <code>{i.check} {i.subject}</code></p>
      <p>{t("incident.opened")}: {when(i.opened_at)} · {age(i.opened_at)}</p>
      <p>{t("mon.opening_value")}: <code>{opening?.payload?.value ?? t("mon.unknown")}</code></p>
      {#if content}
        <p>{t("mon.hash.baseline")}: <code>{i.baseline_value ?? t("mon.unknown")}</code></p>
        <p>{t("mon.hash.now")}: <code>{latest.value || i.value || "—"}</code></p>
      {/if}
      <p>{t("mon.confirmed_checks")}: {i.confirmation_count || t("mon.count_not_started")}
        {#if i.confirmation_started_at}<span class="monitor-note"> · {t("mon.counted_since", {at:exactWhen(i.confirmation_started_at)})}</span>{/if}</p>
      <p>{t("incident.max_level")}: {levelText(i.max_level)}</p>
    </details>
    <section class="monitor-section"><h2>{t("incident.timeline")}</h2>
      <ol class="monitor-rows">{#each page.timeline as event (event.seq)}<li class="monitor-row">
        <div class="monitor-meta"><span>{when(event.payload?.at ?? event.ts)}</span><b>{event.actor === "system:monitor" ? t("mon.source") : event.actor_name || actorName(event.actor)}</b></div>
        <p>{describeEvent(event)}</p>
      </li>{/each}</ol>
    </section>

    {#if sheet === "accept"}<Sheet title={acceptTitle} onclose={() => sheet = null}>
      <!-- what is accepted and what changes, in words; the hashes under "Details" -->
      {#if weakLogin}<p>{t("mon.weak_login_help", {server:i.server})}</p>{/if}
      <p><b>{checkTitle(i.check, i.subject)}</b> · {i.server}</p>
      {#if content}<p>{changeText(i, changes)}</p>{/if}
      <p class="monitor-note">{t("mon.accept.what")}</p>
      {#if i.check === "logins" && !weakLogin}<label class="field"><span>{t("mon.accept.login_label")}</span><input type="text" bind:value={label} maxlength="200" /></label>{/if}
      {#if content}<details class="monitor-details"><summary>{t("mon.accept.details")}</summary>
        <p>{t("mon.hash.baseline")}: <code>{i.baseline_value ?? t("mon.unknown")}</code></p>
        <p>{t("mon.hash.now")}: <code>{latest.value || i.value || "—"}</code></p>
      </details>{/if}
      {#if acceptError}<p class="error-text" role="alert">{acceptError}</p>{/if}
      <div class="actions">
        <button type="button" class="btn" disabled={busy} onclick={() => sheet = null}>{t("action.cancel")}</button>
        <button type="button" class="btn primary" disabled={busy} onclick={() => accept()}>{t("mon.accept.sign")}</button>
      </div>
    </Sheet>{/if}
    {#if sheet === "revoke"}<Sheet title={t("trust.button.revoke")} onclose={() => sheet = null}>
      <!-- nothing is trusted; the monitor checks the server's latest key snapshot before it closes -->
      <p><b>{checkTitle(i.check, i.subject)}</b> · {i.server}</p>
      <p class="monitor-note">{t("mon.revoke.what", {server:i.server})}</p>
      {#if acceptError}<p class="error-text" role="alert">{acceptError}</p>{/if}
      <div class="actions">
        <button type="button" class="btn" disabled={busy} onclick={() => sheet = null}>{t("action.cancel")}</button>
        <button type="button" class="btn primary" disabled={busy} onclick={() => accept("revoke")}>{t("mon.revoke.sign")}</button>
      </div>
    </Sheet>{/if}
    {#if sheet === "quiet"}<Sheet title={t("mon.quiet.button")} onclose={() => sheet = null}>
      <p class="monitor-note">{t("mon.quiet.help")}</p>
      <div class="incident-quiet" role="group" aria-label={t("mon.quiet.button")}>
        {#each MUTES as span}<button type="button" class="btn" disabled={busy} onclick={() => act("mute", {for:span})}>{t(`incident.mute.${span}`)}</button>{/each}
        <button type="button" class="btn" disabled={busy} onclick={() => act("ack")}>{t("mon.quiet.closed")}</button>
      </div>
    </Sheet>{/if}
    {#if sheet === "handoff"}<Sheet title={handoffTitle} onclose={() => sheet = null}>
      <form class="monitor-form" onsubmit={e => {e.preventDefault(); void act("handoff", {project, comment});}}>
        <p class="monitor-note">{t("mon.handoff_help")}</p>
        <label>{t("mon.project")}<select bind:value={project}>{#each availableProjects as p (p.id)}<option value={p.id}>{p.name}</option>{/each}</select></label>
        <label>{t("mon.comment")}<textarea rows="3" bind:value={comment} maxlength="2000"></textarea></label>
        <button class="btn primary" disabled={busy || !project}>{t("mon.send_investigation")}</button>
      </form>
    </Sheet>{/if}
    {#if sheet === "link"}<Sheet title={t("mon.link_existing")} onclose={() => sheet = null}>
      <form class="monitor-form" onsubmit={e => {e.preventDefault(); void previewLink();}}>
        <label>{t("mon.existing_id")}<input type="text" bind:value={existingId} placeholder={`${session.me?.cards ?? "KD"}-123`} pattern={cardFieldPattern(session.me?.cards ?? "KD")} required oninput={() => existing = null} /></label>
        <button class="btn" disabled={busy}>{t("mon.preview_card")}</button>
        {#if existing}
          <p>{existing.id} · {existing.title} · {existing.assignee}</p>
          {#if session.can("cards.edit", existing.project)}
            <label><span><input type="checkbox" bind:checked={transfer} /> {t("mon.transfer_existing")}</span></label>
            <button type="button" class="btn primary" disabled={busy} onclick={() => act("link", {card:existing!.id, revision:existing!.revision, transfer})}>{t("mon.confirm_link")}</button>
          {/if}
        {/if}
      </form>
    </Sheet>{/if}
  {/if}
</div>
{#if signing}<SignDialog items={[signing]} onclose={() => signing = null} ondone={signed} onreread={signed} />{/if}
