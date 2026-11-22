// The monitor's trust by the owner's signature: a monitor_trust decision in words — what becomes
// trusted, how the monitor applied it — and the owner's buttons: "Trust" on a P-n, "Capture a baseline" and "Add an
// address" on a server create the decision (or find the same open proposal) and sign it right away; "Remove" takes
// an address off the trusted list without a signature.
import { apiGet, apiPost } from "./api";
import { t, type Key } from "./i18n.svelte";
import { checkName } from "./servers";
import { when } from "./time";
import type { DecisionItem, TrustApplyState, TrustKind, TrustRemoval } from "./types";

export interface TrustObject {
  kind?: TrustKind;
  incident?: number | { id: string; check?: string; subject?: string };
  id?: string;
  server?: string;
  cidr?: string;
  label?: string;
  check?: string;
  subject?: string;
}

function incidentId(p: TrustObject): string {
  if (typeof p.incident === "object" && p.incident) return p.incident.id;
  if (typeof p.incident === "number") return `P-${p.incident}`;
  return p.id ?? "";
}

/** What becomes trusted, short: "P-7 into the baseline", "the baseline of node-c", "the trusted address 10.1.2.3 ("office")". */
export function trustWhat(p: TrustObject): string {
  if (p.kind === "accept") return t("trust.what.accept", { id: incidentId(p) });
  if (p.kind === "revoke") return t("trust.what.revoke", { id: incidentId(p) });
  if (p.kind === "baseline") return t("trust.what.baseline", { server: serverName(p.server) });
  if (p.kind === "trust") return t("trust.what.trust", { cidr: p.cidr ?? "", label: p.label ?? "" });
  return "";
}

/** The decision's title: the kind and its object. */
export function trustTitle(p: TrustObject): string {
  if (p.kind === "accept") {
    const inc = typeof p.incident === "object" && p.incident ? p.incident : null;
    if ((inc?.check ?? p.check) === "logins" && /@(password|key \?)$/.test(inc?.subject ?? p.subject ?? "")) {
      return t("mon.accept_login_title", {id:incidentId(p)});
    }
    const what = checkName(inc?.check ?? p.check ?? "", inc?.subject ?? p.subject ?? "");
    return t("trust.title.accept", { id: incidentId(p), what });
  }
  if (p.kind === "revoke") {
    const inc = typeof p.incident === "object" && p.incident ? p.incident : null;
    return t("trust.title.revoke", { id: incidentId(p), what: checkName(inc?.check ?? p.check ?? "", inc?.subject ?? p.subject ?? "") });
  }
  if (p.kind === "baseline") return t("trust.title.baseline", { server: serverName(p.server) });
  return t("trust.title.trust", { cidr: p.cidr ?? "", label: p.label ?? "" });
}

function serverName(server: string | undefined): string {
  return server === "all" ? t("trust.all") : (server ?? "");
}

/** How the monitor applied a signed decision; "" — not signed (or nothing to say). */
export function applyLine(state: TrustApplyState | undefined, detail = "", at = ""): string {
  if (state === "pending") return t("trust.apply.pending");
  if (state === "applied") return t("trust.apply.applied", { when: at ? when(at) : "" }) + (detail ? ` — ${detail}` : "");
  if (state === "error") return t("trust.apply.error", { detail });
  return "";
}

export function removalLine(r: TrustRemoval): string {
  return t(`trust.removal.${r.state}` as Key, { detail: r.detail });
}

export interface TrustRequest {
  kind: TrustKind;
  incident?: string;
  server?: string;
  cidr?: string;
  label?: string;
}

/** Create (or find) the decision and read it as the decisions page shows it — ready for the signing dialog. */
export async function propose(request: TrustRequest): Promise<DecisionItem> {
  const answer = await apiPost<{ decision: { id: string } }>("/api/monitor-trust", request);
  return apiGet<DecisionItem>(`/api/decisions/${answer.decision.id}`);
}

/** "Accept into the baseline" (or "Access revoked", kind revoke) for many P-n — one bundle (the server opens
 * or finds each decision), every decision read for one signature; what cannot be proposed comes back with its reason. */
export async function proposeMany(
  ids: string[],
  post: typeof apiPost = apiPost,
  get: typeof apiGet = apiGet,
  kind: "accept" | "revoke" = "accept",
): Promise<{ items: DecisionItem[]; refused: { incident: string; error: string }[] }> {
  const answer = await post<{ decisions: { id: string }[]; refused: { incident: string; error: string }[] }>(
    "/api/monitor-trust/batch", kind === "accept" ? { incidents: ids } : { incidents: ids, kind });
  const items: DecisionItem[] = [];
  for (const d of answer.decisions) items.push(await get<DecisionItem>(`/api/decisions/${d.id}`));
  return { items, refused: answer.refused };
}

export async function removeAddress(cidr: string): Promise<TrustRemoval> {
  return (await apiPost<{ removal: TrustRemoval }>("/api/monitor-trust/remove", { cidr })).removal;
}

/** A journal event of the monitor's trust in words (the feed, the P-n's timeline). */
export function describeTrust(kind: string, p: Record<string, any>): string {
  const step = kind.slice("monitor_trust.".length);
  return t(`feed.monitor_trust.${step}` as Key, { what: trustWhat(p), cidr: p.cidr ?? "", detail: p.detail ?? "" });
}
