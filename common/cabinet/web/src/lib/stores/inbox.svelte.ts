// What waits for the person: decisions they may take (with each one's view and card), agents' questions,
// cards in Review. Review cards can be moved in a batch, one PATCH per card with its own revision.
import { ApiError, apiGet, apiPost } from "../api";
import type { CardCompact, Decision, DecisionItem, DecisionView, Incident } from "../types";

export interface InboxRow {
  decision: Decision;
  view: DecisionView;
  card?: CardCompact;
}

// the order of the groups: what is most dangerous first
const TYPE_ORDER = ["admin_critical", "admin_plan", "delegate", "pr_approve", "monitor_trust", "promote", "permission"];

export function groupByType(rows: InboxRow[]): { type: string; rows: InboxRow[] }[] {
  const groups = new Map<string, InboxRow[]>();
  for (const row of rows) {
    const list = groups.get(row.decision.type) ?? [];
    list.push(row);
    groups.set(row.decision.type, list);
  }
  const rank = (type: string) => {
    const at = TYPE_ORDER.indexOf(type);
    return at < 0 ? TYPE_ORDER.length : at;
  };
  return [...groups.entries()]
    .sort(([a], [b]) => rank(a) - rank(b))
    .map(([type, list]) => ({ type, rows: list }));
}

/** the decisions one batch accept proposed (a common `payload.bundle`) — one row where the first stands,
 * its `members` all of them; the click picks them all for one signature. */
export function bundleRows(rows: InboxRow[]): (InboxRow & { members?: InboxRow[] })[] {
  const out: (InboxRow & { members?: InboxRow[] })[] = [];
  const bundles = new Map<string, InboxRow & { members: InboxRow[] }>();
  for (const row of rows) {
    const bundle = row.decision.payload?.bundle;
    if (typeof bundle !== "string" || !bundle) {
      out.push(row);
      continue;
    }
    const first = bundles.get(bundle);
    if (first) first.members.push(row);
    else {
      const added = { ...row, members: [row] };
      bundles.set(bundle, added);
      out.push(added);
    }
  }
  return out;
}

/** How many rows the decisions are, as the inbox shows them: a bundle counts once. */
export const rowCount = (rows: InboxRow[]) => bundleRows(rows).length;

/** A batch holds open decisions of one type that still match what they were opened over; a pull request is
 * signed alone, with its mode. */
export function canSelect(selected: InboxRow[], row: InboxRow): boolean {
  if (row.view.stale || row.view.blocked || row.decision.state !== "open" || row.decision.type === "pr_approve") return false;
  return selected.length === 0 || selected[0].decision.type === row.decision.type;
}

export type Outcome = "ok" | "conflict" | "error";

export interface TransitionResult {
  card: string;
  outcome: Outcome;
  error?: ApiError | unknown;
  updated?: CardCompact;
}

type Post = (path: string, body: unknown) => Promise<{ card: CardCompact }>;

/** One card action on each card in turn with its own revision (accept, or return with one shared comment); a
 * conflict or a refusal on one does not stop the others. */
export async function actMany(
  cards: CardCompact[],
  action: string,
  comment?: string,
  post: Post = apiPost,
): Promise<TransitionResult[]> {
  const results: TransitionResult[] = [];
  for (const card of cards) {
    try {
      const answer = await post(`/api/cards/${encodeURIComponent(card.id)}/act`, {
        action,
        revision: card.revision,
        ...(comment !== undefined ? { comment } : {}),
      });
      results.push({ card: card.id, outcome: "ok", updated: answer.card });
    } catch (exc) {
      const conflict = exc instanceof ApiError && exc.status === 409;
      results.push({ card: card.id, outcome: conflict ? "conflict" : "error", error: exc });
    }
  }
  return results;
}

interface InboxAnswer {
  decisions: Decision[];
  questions: Decision[];
  review: CardCompact[];
  incidents?: Incident[]; // open crit nobody acknowledged or muted (an older cabinet sends none)
}

class Inbox {
  decisions = $state<InboxRow[]>([]);
  questions = $state<InboxRow[]>([]);
  review = $state<CardCompact[]>([]);
  incidents = $state<Incident[]>([]);
  loaded = $state(false);

  async load() {
    const [mine, views, cards] = await Promise.all([
      apiGet<InboxAnswer>("/api/inbox"),
      apiGet<DecisionItem[]>("/api/decisions?state=open"),
      apiGet<CardCompact[]>("/api/cards"),
    ]);
    const viewOf = new Map(views.map((item) => [item.decision.id, item.view]));
    const cardOf = new Map(cards.map((card) => [card.id, card]));
    const row = (decision: Decision): InboxRow => ({
      decision,
      view: viewOf.get(decision.id) ?? {},
      card: cardOf.get(decision.card),
    });
    this.decisions = mine.decisions.map(row);
    this.questions = mine.questions.map(row);
    this.review = mine.review;
    this.incidents = mine.incidents ?? [];
    this.loaded = true;
  }
}

export const inbox = new Inbox();
