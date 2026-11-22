// Incident groups and bulk actions: the incidents one monitor tick opened for one check are one row
// (`group` from the server); the person picks rows or whole groups and acknowledges, mutes or accepts them at once.
import { apiPost } from "./api";
import type { Incident, Level } from "./types";

export type Row =
  | { kind: "one"; incident: Incident }
  | {
      kind: "group";
      id: string;
      size: number;
      servers: number;
      subjects: number;
      check: string;
      subject: string;
      level: Level;
      opened_at: string;
      members: Incident[];
    };

const RANK: Record<string, number> = { ok: 0, warn: 1, crit: 2 };

/** The rows as the page shows them: a group's members fold into its row where the first of them stands. */
export function groupRows(items: Incident[]): Row[] {
  const rows: Row[] = [];
  const groups = new Map<string, Extract<Row, { kind: "group" }>>();
  for (const i of items) {
    if (!i.group) {
      rows.push({ kind: "one", incident: i });
      continue;
    }
    const row = groups.get(i.group.id);
    if (row) {
      row.members.push(i);
      if ((RANK[i.level] ?? 0) > (RANK[row.level] ?? 0)) row.level = i.level;
      continue;
    }
    const added = {
      kind: "group" as const, id: i.group.id, size: i.group.size, servers: i.group.servers,
      subjects: i.group.subjects, check: i.check, subject: i.subject, level: i.level, opened_at: i.opened_at,
      members: [i],
    };
    groups.set(i.group.id, added);
    rows.push(added);
  }
  return rows;
}

/** The incidents page narrowed to one group. */
export const groupHref = (id: string) => `/incidents?group=${encodeURIComponent(id)}`;

/** What the owner may accept into the baseline (as monitor_trust.acceptable on the server). */
export function acceptable(i: Incident): boolean {
  if (i.status === "resolved" || i.accepted) return false; // signed already, the monitor applies it
  if (i.check === "logins") return i.subject !== "gap";
  return i.check.startsWith("sec:") || i.check === "ports" || i.check === "wgpeers";
}

/** What may close as "access revoked" (as monitor_trust.revocable on the server): a login by a key. */
export function revocable(i: Incident): boolean {
  if (i.status === "resolved" || i.accepted || i.check !== "logins") return false;
  return i.subject !== "gap" && !/@(password|key \?)$/.test(i.subject);
}

/** One signature takes at most this many decisions (api.BATCH_MAX on the server). */
export const BATCH_MAX = 50;

/** The acceptable ones of the picked incidents, the first BATCH_MAX of them; `over` — how many wait for the next
 * signature. */
export function toAccept(items: Incident[], can: (i: Incident) => boolean = acceptable): { ids: string[]; over: number } {
  const ids = items.filter(can).map((i) => i.id);
  return { ids: ids.slice(0, BATCH_MAX), over: Math.max(0, ids.length - BATCH_MAX) };
}

export type BulkAction = "ack" | "mute" | "unmute";

export interface BulkResult {
  id: string;
  ok: boolean;
  error?: unknown;
}

/** One action on each incident in turn through its own route; a refusal does not stop the rest. */
export async function runMany(
  ids: string[],
  action: BulkAction,
  body: object = {},
  post: (path: string, body?: unknown) => Promise<unknown> = apiPost,
): Promise<BulkResult[]> {
  const out: BulkResult[] = [];
  for (const id of ids) {
    try {
      await post(`/api/incidents/${id}/${action}`, body);
      out.push({ id, ok: true });
    } catch (error) {
      out.push({ id, ok: false, error });
    }
  }
  return out;
}
