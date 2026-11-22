// Journal events in words, for a card's history and the feed. The server stores keys and facts only;
// the reader's language turns them into text here.
import { hasKey, t, type Key } from "./i18n.svelte";
import { projects } from "./stores/projects.svelte";
import { when } from "./time";
import { describeTrust } from "./trust";
import type { JournalEvent } from "./types";
import { checkTitle, levelText } from "./monitoring";

/** Where an incident P-n opens: its page in the cabinet. */
export { incidentHref } from "./servers";

/** The incident P-n a journal event is about, or "". */
export function incidentOf(event: JournalEvent): string {
  return event.kind.startsWith("incident.") && typeof event.payload?.id === "string" ? event.payload.id : "";
}

/** `user:owner` → `@owner`, `agent:dev` → `@dev`, `system` and `system:diag` → "System". */
export function actorName(actor: string | undefined): string {
  if (!actor) return "";
  if (actor === "system" || actor.startsWith("system:")) return t("feed.system");
  return actor.replace(/^(user|agent):/, "@");
}

function decisionType(type: unknown): string {
  return hasKey(`decision.type.${type}`) ? t(`decision.type.${type}` as Key) : String(type ?? "");
}

/** A thread message another write stands for (an edit's comment, a decision's line): history and the feed
 * show that write once; the thread still takes the message. */
export function isEcho(event: JournalEvent): boolean {
  if (event.kind.startsWith("voice.") || (event.kind === "message.changed" && !!event.payload?.voice)) return true; // the transcription line's own steps
  return event.kind === "message.added" && !!event.payload?.via;
}

const TOOL_KINDS = new Set(["tool_call", "tool_result"]);

/** A tool call or its result: a turn's inner steps, which change no summary screen (the overview, the agents). */
export function isToolMessage(event: JournalEvent): boolean {
  return event.kind === "message.added" && TOOL_KINDS.has(event.payload?.kind);
}

export function describeEvent(event: JournalEvent): string {
  const p = event.payload ?? {};
  switch (event.kind) {
    case "llm_key.changed":
      return t(p.action === "delete" ? "feed.llm_key.deleted" : "feed.llm_key.updated", { provider: p.provider ?? "" });
    case "card.created":
      return t("feed.card.created", { title: p.title ?? "" });
    case "card.changed": {
      const from = p.before?.status;
      const to = p.after?.status;
      if (from && to && from !== to) return `${from} → ${to}`;
      return t("history.card.changed", { fields: (p.fields ?? []).join(", ") });
    }
    case "message.added":
      return hasKey(`feed.message.${p.kind}`) ? t(`feed.message.${p.kind}` as Key) : t("history.message.added");
    case "user.role_set": // a role's permissions ({role}) or a person's roles ({user})
      return p.role ? t("feed.user.role_set", { role: p.role }) : t("feed.user.roles", { user: p.user ?? "" });
    case "user.code_issued": // who gave it (the actor) to whom, until when; never the code
      return t("feed.user.code_issued", { user: p.user ?? "", until: p.expires ? when(p.expires) : "" });
    case "digest": {
      const text = typeof p.key === "string" && hasKey(p.key) ? t(p.key as Key, p.params ?? {}) : t("feed.digest");
      // the servers' part, to who reads the servers
      return p.incidents ? `${text} · ${t("push.digest.body.incidents", p.incidents)}` : text;
    }
    case "incident.opened": // the check with its subject, as the push names it
      return t("feed.incident.opened", { level: levelText(p.level ?? ""), server: p.server ?? "—", value: p.value ?? "—",
        check: p.check ? checkTitle(p.check, p.subject ?? "") : "—" });
    case "incident.assigned":
      return t("mon.event.assigned", { card: p.card ?? "" });
    case "incident.unacknowledged":
      return t("mon.event.unack");
    case "incident.fixed": // the monitor's import brings a fix without its card
      return p.card ? t("feed.incident.fixed", { card: p.card }) : t("feed.incident.fixed_bare");
    case "incident.window": // a dangerous change of a plan step, linked to its card
      return t("feed.incident.window", { card: p.card ?? "—" });
    case "incident.resolved": // the owner switched its check off on the server; access removed
      if (p.how === "disabled") return t("feed.incident.resolved_disabled");
      return p.how === "revoke" ? t("feed.incident.resolved_revoke") : t("feed.incident.resolved");
    case "monitor.settings": // a save of the monitor's settings — its keys (a server's in brackets), signed
      return t(p.signed ? "feed.monitor.settings_signed" : "feed.monitor.settings", {
        keys: Array.isArray(p.changes) ? p.changes.map((c: { key: string; server?: string }) =>
          c.server ? `${c.key} (${c.server})` : c.key).join(", ") : "",
      });
    case "incident.muted":
      return t("feed.incident.muted", { until: p.until ? when(p.until) : "" });
    case "card.deferred": // put off until a time, by whom (the actor) and why
      return t("feed.card.deferred", { until: p.until ? when(p.until) : "", reason: p.reason ?? "" });
    case "card.undeferred":
      return hasKey(`feed.card.undeferred.${p.how}`) ? t(`feed.card.undeferred.${p.how}` as Key) : t("feed.card.undeferred.cleared");
    case "server.check": // "Check now": queued, running, done or failed
      return t("feed.server.check", {
        server: p.server ?? "",
        state: hasKey(`server.check.state.${p.state}`) ? t(`server.check.state.${p.state}` as Key) : String(p.state ?? ""),
      });
    case "notice": // a root service's or an executor's notice: its catalog key and parameters
      return typeof p.key === "string" && hasKey(p.key) ? t(p.key as Key, p.params ?? {}) : t("feed.notice");
    case "project.created": // by name; a deleted project has only its id left
      return t("feed.project.created", { project: p.name ?? p.project ?? "" });
    case "project.changed":
      return t("feed.project.changed", { project: projects.name(p.project ?? "") });
    case "project.deleted":
      return t("feed.project.deleted", { project: p.project ?? "", target: projects.name(p.target ?? "") });
    case "project.repo": // a repository request or the root unit's outcome; "" — unbound
      return p.state && hasKey(`projects.repo_state.${p.state}`)
        ? t("feed.project.repo", {
            project: projects.name(p.project ?? ""),
            repo: p.repo ?? "",
            state: t(`projects.repo_state.${p.state}` as Key),
          })
        : t("feed.project.repo_unbound", { project: projects.name(p.project ?? "") });
  }
  if (event.kind.startsWith("monitor_trust.") && hasKey(`feed.${event.kind}`)) return describeTrust(event.kind, p);
  if (event.kind.startsWith("decision.")) {
    const key = `sys.decision.${event.kind.slice("decision.".length)}`;
    if (hasKey(key)) return t(key as Key, { by: actorName(event.actor), type: decisionType(p.type) });
  }
  const key = `feed.${event.kind}`;
  return hasKey(key) ? t(key as Key, p) : event.kind;
}
