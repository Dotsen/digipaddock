// Approving decisions (server: web/decisions.py): a challenge over the decisions as the page shows them,
// then approve with a proof — a passkey assertion over it, or a TOTP code; a type without step-up needs
// none. A challenge that expired (410) is asked again once; a conflict (409) goes back to the page — and a
// challenge over a card revision other than the one shown is a conflict before any biometric prompt.
import { ApiError, apiPost } from "./api";
import type { DecisionItem, PrMode } from "./types";
import { assertPasskey } from "./webauthn";

export interface Challenge {
  challenge_id: string;
  options: Record<string, unknown> | null;
  step_up: boolean;
  totp: boolean;
  expires: string;
  revisions: Record<string, number | string>;
}

export type Proof = { credential?: unknown; totp?: string };

/** How often a page re-reads a decision that cannot be approved for now (`view.blocked`). */
export const BLOCKED_RETRY_MS = 30_000;
export type Prove = (challenge: Challenge) => Promise<Proof>;

/** The person has no signing passkey for this challenge; `totp` says whether a code would do. */
export class NoPasskey extends Error {
  constructor(public totp: boolean) {
    super("no signing passkey");
    this.name = "NoPasskey";
  }
}

export const withPasskey: Prove = async (challenge) => {
  if (!challenge.options) throw new NoPasskey(challenge.totp);
  return { credential: await assertPasskey(challenge.options) };
};

export const withTotp =
  (code: string): Prove =>
  async () => ({ totp: code });

/** The card revisions the page shows for the items (a view without its card binds nothing), and the deployed tree
 *  a factory plan's component step is shown signed for (`deployed:<card>`). */
export function shownRevisions(items: DecisionItem[]): Record<string, number | string> {
  const shown: Record<string, number | string> = {};
  for (const item of items) {
    const revision = item.view?.card?.revision;
    if (typeof revision === "number") shown[item.decision.card] = revision;
    const deployed = item.view?.deployed;
    if (typeof deployed === "string" && deployed) shown[`deployed:${item.decision.card}`] = deployed;
  }
  return shown;
}

function stale(challenge: Challenge, shown: Record<string, number | string>): string | null {
  for (const [card, revision] of Object.entries(shown)) {
    const bound = challenge.revisions?.[card];
    if (bound !== undefined && bound !== revision) return card;
  }
  return null;
}

async function once(ids: string[], shown: Record<string, number | string>, prove: Prove, ttl?: number, mode?: PrMode) {
  const challenge = await apiPost<Challenge>("/api/decisions/challenge", { ids, ...(mode ? { mode } : {}) });
  const changed = stale(challenge, shown);
  if (changed)
    throw new ApiError(
      409,
      "conflict",
      changed.startsWith("deployed:")
        ? `${changed.slice(9)}: a new release is deployed since the page was read`
        : `${changed}: the card changed since the page was read`,
    );
  const proof = challenge.step_up ? await prove(challenge) : {};
  return apiPost("/api/decisions/approve", {
    challenge_id: challenge.challenge_id,
    ...proof,
    ...(ttl ? { ttl_hours: ttl } : {}),
  });
}

/** `mode` — what a signature on a pull request asks for; the server binds it into the signed value. */
export async function approveDecisions(items: DecisionItem[], prove: Prove, ttl?: number, mode?: PrMode) {
  const ids = items.map((item) => item.decision.id);
  const shown = shownRevisions(items);
  try {
    return await once(ids, shown, prove, ttl, mode);
  } catch (exc) {
    if (exc instanceof ApiError && exc.status === 410) return once(ids, shown, prove, ttl, mode);
    throw exc;
  }
}
