import { hasKey, t, type Key } from "./i18n.svelte";
import { exactWhen, when } from "./time";
import type { Incident, IncidentChanges, IncidentPage, LinkedCard, ServerCheck } from "./types";

export const activeCards = (cards: LinkedCard[]) => cards.filter(c => !["Done", "Cancelled"].includes(c.status));

/** The `pattern` of a field that takes a card: the host's prefix in either case, a hyphen, the number. */
export const cardFieldPattern = (prefix: string) =>
  [...prefix].map(c => (/[A-Z]/.test(c) ? `[${c}${c.toLowerCase()}]` : c)).join("") + "-[0-9]+";
export const activeIncidents = (items: Incident[]) => items.filter(i => i.status !== "resolved");
export const isDiagnostic = (check: string) => check.startsWith("gate:") || check === "health";
export const needsAttention = (c: ServerCheck) => c.freshness !== "disabled" &&
  (c.level !== "ok" || ["stale", "error", "unknown"].includes(c.freshness ?? "fresh"));

export function checkTitle(check: string, subject = ""): string {
  const key = `mon.check.${check}`;
  const name = hasKey(key) ? t(key) : check.startsWith("gate:")
    ? t("mon.check.gate", { check: checkTitle(check.slice(5)) }) : check;
  return subject && subject !== "-" ? `${name} · ${subject}` : name;
}

export const levelText = (level: string) => hasKey(`servers.level.${level}`)
  ? t(`servers.level.${level}` as Key) : t("mon.unknown");

export function notificationText(i: Incident, now = Date.now()): string {
  const parts: string[] = [];
  if (i.status === "acknowledged") parts.push(t("mon.acknowledged"));
  if (i.muted_until && Date.parse(i.muted_until) > now) parts.push(t("mon.muted_until", { at: when(i.muted_until) }));
  return parts.join(" · ");
}

export function resolutionText(i: Incident): string {
  const key = `mon.resolution.${i.resolution || "unknown"}`;
  return hasKey(key) ? t(key) : t("mon.closed");
}

export function explanation(i: Pick<Incident, "check" | "subject" | "value" | "text">): string {
  if (i.check === "units") return t(i.subject === "certbot.service" ? "mon.explain.certbot" : "mon.explain.units", {unit:i.subject});
  if (i.check === "sec:units" && i.value.includes("symlink -> /dev/null")) return t("mon.explain.masked");
  if (i.check.startsWith("sec:")) return t("mon.explain.drift");
  if (i.check === "reboot") return t("mon.explain.reboot");
  if (i.check === "logins") return t("mon.explain.login");
  if (i.check === "ports" || i.check === "wgpeers") return t("mon.explain.object");
  if (isDiagnostic(i.check)) return t("mon.explain.collection");
  return t("mon.explain.threshold");
}

/** What the incident's page asks of the owner: its live card, or by its check — a difference from
 * the baseline is his own change or not; a threshold waits for recovery or for his reading of the investigation. */
export type Ask =
  | { step: "closed" }
  | { step: "accepting"; at: string; revoke?: boolean }
  | { step: "card"; card: LinkedCard }
  | { step: "confirm_change"; login: boolean; failed?: string; revokeFailed?: boolean }
  | { step: "review_report" }
  | { step: "investigating" }
  | { step: "wait_recovery" };

export function incidentAsk(page: Pick<IncidentPage, "incident" | "cards" | "acceptable" | "trust">): Ask {
  const i = page.incident;
  if (i.status === "resolved") return { step: "closed" };
  // the owner's last signed "accept" (or "access revoked"): the monitor applies it on its next tick — until
  // then nothing to ask again
  const signed = (page.trust ?? []).filter(d => (d.payload.kind === "accept" || d.payload.kind === "revoke") && d.state === "approved")
    .sort((a, b) => b.created.localeCompare(a.created))[0];
  const revoke = signed?.payload.kind === "revoke";
  if (signed?.apply_state === "pending") {
    return { step: "accepting", at: signed.decided_at || signed.created, ...(revoke ? { revoke } : {}) };
  }
  const work = activeCards(page.cards);
  if (work.length) return { step: "card", card: work[0] };
  if (page.acceptable) {
    const failed = signed?.apply_state === "error"
      ? { failed: signed.apply_detail, ...(revoke ? { revokeFailed: true } : {}) } : {};
    return { step: "confirm_change", login: i.check === "logins", ...failed };
  }
  if (i.investigation?.report) return { step: "review_report" };
  // «waiting»: the monitor's wake is due (it wakes the sysadmin on each opening); «failed»: nobody is on it
  const state = i.investigation?.state;
  return state && state !== "failed" ? { step: "investigating" } : { step: "wait_recovery" };
}

/** The "Actions" menu: what the question's own buttons do not offer — accepting when a card leads, the one
 * "Do not remind" (or turning reminders back on), the server's recheck, linking a card. */
export type IncidentAction = "accept" | "revoke" | "quiet" | "unquiet" | "recheck" | "link";

export function incidentActions(
  ask: Ask, may: { canTrust: boolean; canRevoke?: boolean; canAct: boolean; quiet: boolean },
): IncidentAction[] {
  if (ask.step === "closed") return [];
  const out: IncidentAction[] = [];
  if (may.canTrust && ask.step !== "confirm_change" && ask.step !== "accepting") out.push("accept");
  // a login by a key whose access is gone — offered beside accept, never instead of the question's buttons
  if (may.canRevoke && ask.step !== "confirm_change" && ask.step !== "accepting") out.push("revoke");
  if (may.canAct) out.push(may.quiet ? "unquiet" : "quiet", "recheck");
  out.push("link");
  return out;
}

/** A difference's content in words: when it first changed and how often after the opening — the owner
 * accepts what is there now; hashes stay in the technical details. */
export function changeText(i: Pick<Incident, "opened_at">, changes: IncidentChanges): string {
  const first = t("mon.change.first", { at: exactWhen(i.opened_at) });
  return changes.count
    ? `${first} ${t("mon.change.again", { count: changes.count, at: exactWhen(changes.last_at) })}`
    : first;
}

/** The one "Do not remind": until closed (acknowledged) or until a time, or null — reminders are on. */
export function quietState(i: Pick<Incident, "status" | "muted_until">, now = Date.now()): { until: string } | null {
  if (i.status === "acknowledged") return { until: "closed" };
  return i.muted_until && Date.parse(i.muted_until) > now ? { until: i.muted_until } : null;
}

export function thresholdLine(c: ServerCheck): string {
  const limit = c.thresholds;
  if (!limit || (limit.warn === undefined && limit.crit === undefined)) return "";
  const unit = t(`mon.unit.${limit.unit}` as Key);
  return [limit.warn !== undefined ? t("mon.warn_threshold", {value:limit.warn, unit}) : "",
    limit.crit !== undefined ? t("mon.crit_threshold", {value:limit.crit, unit}) : ""].filter(Boolean).join(" · ");
}

export interface IncidentQuery {
  items: Incident[];
  total: number;
  counts: Record<string, number>;
  next_cursor: string;
}
