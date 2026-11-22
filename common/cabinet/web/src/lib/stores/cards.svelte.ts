// The board: the visible cards of a project (or all), filtered on the page; live card events keep it current.
import { apiGet } from "../api";
import { STATUSES, compact, type CardCompact, type JournalEvent, type Status } from "../types";

export interface BoardQuery {
  project?: string;
  q?: string;
}

export interface BoardFilters {
  assignee?: string;
  spec?: string;
  label?: string;
  /** One board column; "" shows all except Cancelled. */
  status?: Status | "";
}

/** The board's `status` parameter: a column's status, anything else — all. */
export function boardStatus(raw: string | null): Status | "" {
  return (STATUSES as readonly string[]).includes(raw ?? "") ? (raw as Status) : "";
}

/** How many filters behind the phone's "Filters" are set. */
export function activeFilters(filters: BoardFilters): number {
  return [filters.assignee, filters.spec, filters.label, filters.status].filter(Boolean).length;
}

/** The fields a card.changed event carries, applied at once; the rest comes with the refetch. */
export function applyCardEvent(list: CardCompact[], event: JournalEvent): CardCompact[] {
  if (event.kind !== "card.changed" || !event.card) return list;
  const at = list.findIndex((c) => c.id === event.card);
  if (at < 0) return list;
  const after = event.payload?.after ?? {};
  const next = [...list];
  next[at] = {
    ...list[at],
    ...(typeof event.payload?.title === "string" ? { title: event.payload.title } : {}),
    ...(typeof after.status === "string" ? { status: after.status } : {}),
    ...(typeof after.assignee === "string" ? { assignee: after.assignee } : {}),
  };
  return next;
}

export function filterCards(cards: CardCompact[], filters: BoardFilters): CardCompact[] {
  return cards.filter(
    (c) =>
      (!filters.assignee || c.assignee === filters.assignee) &&
      (!filters.spec || c.labels.includes(filters.spec)) &&
      (!filters.label || c.labels.includes(filters.label)) &&
      (filters.status ? c.status === filters.status : c.status !== "Cancelled"),
  );
}

/** The board's columns in order; Cancelled needs an explicit selection. */
export function byStatus(cards: CardCompact[], only: Status | "" = ""): { status: Status; cards: CardCompact[] }[] {
  return STATUSES.filter((status) => only ? status === only : status !== "Cancelled").map((status) => ({
    status,
    cards: cards.filter((c) => c.status === status),
  }));
}

export const DONE_DAYS = 7;

/** Limit Done to the last `days`; other statuses keep their full history. */
export function doneSince(cards: CardCompact[], now: number, days = DONE_DAYS): CardCompact[] {
  const since = now - days * 86_400_000;
  return cards.filter((c) => c.status !== "Done" || Date.parse(c.updated) >= since);
}

function path(query: BoardQuery): string {
  const params = new URLSearchParams();
  if (query.project) params.set("project", query.project);
  if (query.q) params.set("q", query.q);
  const text = params.toString();
  return text ? `/api/cards?${text}` : "/api/cards";
}

class Board {
  cards = $state<CardCompact[]>([]);
  // raw: a deep $state would hand back a proxy, and the "is this still the latest load" check compares identity
  query = $state.raw<BoardQuery>({});
  loading = $state(false);

  async load(query: BoardQuery = {}) {
    this.query = query;
    this.loading = true;
    try {
      const found = await apiGet<CardCompact[]>(path(query));
      if (this.query === query) this.cards = found.map(compact);
    } finally {
      this.loading = false;
    }
  }

  private fits(card: CardCompact): boolean {
    return !this.query.project || card.project === this.query.project;
  }

  /** A card event: applied at once, then the card read again — or, under a search, true: the page reads the
   * search again (coalesced), so a card that came to match appears and one that stopped matching goes. An
   * answer that comes after the query changed (another project, another search) is dropped. */
  async onEvent(event: JournalEvent): Promise<boolean> {
    // a defer set or ended changes the tile's "⏰ until …": the card is read again
    if (!event.card || !["card.changed", "card.created", "card.deferred", "card.undeferred"].includes(event.kind)) return false;
    this.cards = applyCardEvent(this.cards, event);
    if (this.query.q) return true;
    const query = this.query;
    const fresh = compact(await apiGet<CardCompact>(`/api/cards/${encodeURIComponent(event.card)}`));
    if (this.query !== query) return false;
    const at = this.cards.findIndex((c) => c.id === fresh.id);
    if (at >= 0) {
      const next = [...this.cards];
      next[at] = fresh;
      this.cards = this.fits(fresh) ? next : next.filter((c) => c.id !== fresh.id);
    } else if (this.fits(fresh)) {
      this.cards = [...this.cards, fresh];
    }
    return false;
  }
}

export const board = new Board();
