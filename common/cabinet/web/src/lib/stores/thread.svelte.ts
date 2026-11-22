// One card's thread, or an agent's chat: messages by seq. One synced read at a
// time: first the snapshot, then what is new after the last message the server sent. The snapshot merges into what already
// came (the person's own POST), a read of a card the page has left is dropped (the generation), and after
// the snapshot the hub replays the events since its watermark, so nothing written meanwhile is missed. A
// failed read is retried (the stale strip); a failed snapshot is also the thread's `error`. The snapshot is the
// newest window of the thread: older pages come on request, `older` says some are left.
import { apiGet } from "../api";
import { replay } from "../live.svelte";
import { eventSpace, spaceBase } from "../spaces";
import { synced } from "../synced.svelte";
import type { JournalEvent, Message } from "../types";

export function insertMessages(list: Message[], incoming: Message[]): Message[] {
  const bySeq = new Map(list.map((m) => [m.seq, m]));
  for (const message of incoming) bySeq.set(message.seq, message);
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}

/** The most messages one read of the thread brings: the snapshot's window and each older page. */
export const THREAD_PAGE = 200;

/** GET /api/cards/{id}/messages (or /api/agents/{name}/messages): the messages, the journal's seq they are
 * current to and whether older ones are left (a page). */
interface Snapshot {
  messages: Message[];
  watermark: number;
  more?: boolean;
}

class Thread {
  card = $state<string | null>(null);
  /** The agent thread this is (its page; lib/spaces key — the agent, or `<agent>/monitor`); null on a card's page. */
  agent = $state<string | null>(null);
  messages = $state<Message[]>([]);
  loading = $state(false);
  error = $state<unknown>(null);
  /** Older messages are left beyond the first one shown. */
  older = $state(false);
  loadingOlder = $state(false);
  private gen = 0;
  private snapshot = false; // the current card's snapshot has been read
  private cursor = 0; // the last seq the server sent (an own message may skip one written before it)
  private readonly sync = synced(() => this.read());

  get stale() {
    return this.sync.stale;
  }

  /** Open a card's thread: what was shown goes, the card's snapshot is read (retried until it is). */
  load(card: string): Promise<void> {
    return this.open(card, null);
  }

  /** Open an agent's chat, as a card's thread. */
  loadAgent(agent: string): Promise<void> {
    return this.open(null, agent);
  }

  private open(card: string | null, agent: string | null): Promise<void> {
    this.gen += 1;
    this.card = card;
    this.agent = agent;
    this.messages = [];
    this.error = null;
    this.snapshot = false;
    this.cursor = 0;
    this.older = false;
    this.loadingOlder = false;
    this.loading = true;
    return this.sync.run();
  }

  /** The card page closed: reads on their way are dropped, nothing more is read. */
  close() {
    this.gen += 1;
    this.card = null;
    this.agent = null;
    this.loading = false;
  }

  /** The thread's «retry» after a failed first load. */
  retry(): Promise<void> {
    return this.sync.run();
  }

  /** What is new since the last message the server sent (a live event, a decision taken here). */
  fetchNewer(): Promise<void> {
    return this.sync.run();
  }

  /** One older page before the first message shown; a failure is the caller's to tell (the button stays). */
  async loadOlder(): Promise<void> {
    const path = this.path();
    if (!path || !this.older || this.loadingOlder || !this.messages.length) return;
    const gen = this.gen;
    this.loadingOlder = true;
    try {
      const found = await apiGet<Snapshot>(`${path}?before=${this.messages[0].seq}&limit=${THREAD_PAGE}`);
      if (gen !== this.gen) return;
      this.messages = insertMessages(this.messages, found.messages);
      this.older = !!found.more;
    } finally {
      if (gen === this.gen) this.loadingOlder = false;
    }
  }

  /** The open thread's generation: a write takes it before it goes out and gives it back with `add`. */
  get generation(): number {
    return this.gen;
  }

  /** The message a write of this page made, into the thread it was made in: once another thread (or
   * this one again, A → B → A) has opened since, it is dropped — the server keeps it and that thread's own read
   * or its live event brings it. */
  add(message: Message, generation: number) {
    if (generation !== this.gen) return;
    this.messages = insertMessages(this.messages, [message]);
  }

  async onEvent(event: JournalEvent) {
    if (event.kind !== "message.added" && event.kind !== "message.changed") return;
    const mine = this.card
      ? event.card === this.card
      : !!this.agent && !event.card && eventSpace(event.payload) === this.agent;
    if (!mine) return;
    if (event.kind === "message.added") await this.fetchNewer();
    else await this.reread(Number(event.payload?.seq));
  }

  /** One message shown again as it is now (a file of it deleted); one not shown stays unread. */
  async reread(seq: number) {
    const path = this.path();
    if (!path || !Number.isInteger(seq) || !this.messages.some((m) => m.seq === seq)) return;
    const gen = this.gen;
    const found = await apiGet<Snapshot>(`${path}?before=${seq + 1}&limit=1`);
    if (gen !== this.gen) return;
    this.messages = insertMessages(this.messages, found.messages.filter((m) => m.seq === seq));
  }

  private path(): string | null {
    if (this.card) return `/api/cards/${encodeURIComponent(this.card)}/messages`;
    if (this.agent) return `${spaceBase(this.agent)}/messages`;
    return null;
  }

  private async read() {
    const path = this.path();
    if (!path) return;
    const gen = this.gen;
    const first = !this.snapshot;
    try {
      const found = await apiGet<Snapshot>(first ? `${path}?limit=${THREAD_PAGE}` : `${path}?after=${this.cursor}`);
      if (gen !== this.gen) return; // the page moved to another card meanwhile
      this.messages = insertMessages(this.messages, found.messages);
      this.cursor = Math.max(this.cursor, ...found.messages.map((m) => m.seq));
      if (first) {
        this.snapshot = true;
        this.error = null;
        this.older = !!found.more;
        replay(found.watermark);
      }
    } catch (exc) {
      if (gen !== this.gen) return;
      if (first) this.error = exc;
      throw exc;
    } finally {
      if (gen === this.gen) this.loading = false;
    }
  }
}

export const thread = new Thread();
