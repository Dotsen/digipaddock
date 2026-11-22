// The "Servers" section: addresses, the order of servers, an incident's state and "Check now".
// Plain code without the store or i18n, so the pages and the tests share it.
import type { CheckRequest, JournalEvent, ServerRow } from "./types";

/** "Mute" for an hour, a day or a week — what POST /api/incidents/<P-n>/mute takes. */
export const MUTES = ["1h", "1d", "7d"] as const;
export type Mute = (typeof MUTES)[number];

export const incidentHref = (id: string): string => `/incidents/${encodeURIComponent(id)}`;
export const serverHref = (alias: string): string => `/servers/${encodeURIComponent(alias)}`;

/** A check with its subject, as the monitor names it: `disk /`, `reach` (the subject «-» is none). */
export function checkName(check: string, subject: string): string {
  return subject && subject !== "-" ? `${check} ${subject}` : check;
}

/** A check's value for a one-line summary: a digest (a file's hash, 32+ hex digits) is cut to its first 12, as a
 * commit is named; any other value as it is. The incident's page shows the whole digest. */
export function shortValue(value: string | null | undefined): string {
  const text = value ?? "";
  return /^[0-9a-f]{32,}$/i.test(text) ? `${text.slice(0, 12)}…` : text;
}

/** The request still waits for the monitor (queued or running): the button stays off. */
export function checking(request: CheckRequest | null | undefined): boolean {
  return !!request && (request.state === "queued" || request.state === "running");
}

const RANK = { crit: 1, warn: 2, ok: 3, "": 4 } as const;

/** The servers the monitor checks, the worst first: down, crit, warn, ok, not checked yet; switched off last. */
export function sortServers(rows: ServerRow[]): ServerRow[] {
  const rank = (row: ServerRow) => (!row.enabled ? 6 : !row.reachable ? 0 : RANK[row.worst ?? ""] ?? 5);
  return [...rows].sort((a, b) => rank(a) - rank(b) || a.alias.localeCompare(b.alias));
}

/** A live event that may change the servers' screens: an incident's step, a "Check now" step or one of the
 * monitor's trust (proposed, signed, applied, an address removed). */
export function touchesServers(event: Pick<JournalEvent, "kind"> & {payload?: Record<string, any>}): boolean {
  return event.kind.startsWith("incident.") || event.kind === "server.check" || event.kind.startsWith("monitor_trust.") ||
    (event.kind === "message.added" && !!event.payload?.incidents?.length) ||
    (/^(run|command)\./.test(event.kind) && event.payload?.agent === "sysadmin");
}
