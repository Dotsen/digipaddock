// The card's councils: every gathering of its critics, newest first, as the block in the thread shows
// them — and whether this person may cancel one. Read with the card and again on a council's event (a round, a
// critic's run, the writer's reply, the end) or a review's message; a failed read is retried and keeps the last
// list; an answer for another card, or for a page that moved on, is dropped.
import { apiGet, apiPost } from "../api";
import { synced } from "../synced.svelte";
import type { CardCouncils, CouncilView, JournalEvent } from "../types";

const REVIEWS = new Set(["review", "review_reply"]);

class CouncilsStore {
  id = $state<string | null>(null);
  list = $state<CouncilView[]>([]);
  canManage = $state(false);
  /** A cancel is on its way. */
  busy = $state(false);
  private gen = 0;
  private readonly data = synced(() => this.reload());

  load(id: string): Promise<void> {
    this.gen += 1;
    if (this.id !== id) this.show({ councils: [], can_manage: false });
    this.id = id;
    return this.data.run();
  }

  /** The page closed: answers on their way are dropped, events and resyncs read nothing. */
  close() {
    this.gen += 1;
    this.id = null;
    this.show({ councils: [], can_manage: false });
  }

  /** The councils of the card the page shows; none for any other. */
  of(id: string): CouncilView[] {
    return this.id === id ? this.list : [];
  }

  private show(answer: CardCouncils) {
    this.list = answer.councils;
    this.canManage = answer.can_manage;
  }

  private async reload() {
    const id = this.id;
    if (!id) return;
    const gen = this.gen;
    const answer = await apiGet<CardCouncils>(`/api/cards/${encodeURIComponent(id)}/councils`);
    if (gen === this.gen && this.id === id) this.show(answer);
  }

  /** The owner's word: the council is over, its critics stop. The server answers the card's councils after it. */
  async cancel(card: string, council: string): Promise<void> {
    const gen = this.gen;
    this.busy = true;
    try {
      const path = `/api/cards/${encodeURIComponent(card)}/councils/${encodeURIComponent(council)}/cancel`;
      const answer = await apiPost<CardCouncils>(path);
      if (gen === this.gen && this.id === card) this.show(answer);
    } finally {
      this.busy = false;
    }
  }

  async onEvent(event: JournalEvent) {
    if (!this.id || event.card !== this.id) return;
    const review = event.kind === "message.added" && REVIEWS.has(event.payload?.kind);
    if (review || event.kind.startsWith("council.")) await this.data.run();
  }
}

export const councils = new CouncilsStore();
