// Unread replies of agent chats: pure rules shared by the live store, the menu and the thread.
// The server keeps one read cursor per person and chat; a page only reports what it showed.

/** A chat as the server counts it for the person: their cursor, unread replies, the first unread seq, the end. */
export interface ChatState {
  read: number;
  unread: number;
  first: number | null;
  last: number;
}

export type Chats = Record<string, ChatState>;

/** The fresher of two states of one chat: the cursor never goes back, the end never shrinks. A late answer of an
 * older request (or another tab's older view) does not undo what is already known. */
export function fresher(current: ChatState | undefined, next: ChatState): ChatState {
  if (!current) return next;
  if (next.read < current.read) return current;
  if (next.read === current.read && next.last < current.last) return current;
  return next;
}

/** Merge a server snapshot of all chats into what the page holds. */
export function mergeChats(current: Chats, next: Chats): Chats {
  const out: Chats = {};
  for (const [agent, state] of Object.entries(next)) out[agent] = fresher(current[agent], state);
  return out;
}

/** Keep the app name first: Chromium otherwise prefixes installed windows with it a second time. */
export function titleFor(base: string, unread: number): string {
  return unread > 0 ? `${base} (${unread > 99 ? "99+" : unread})` : base;
}

/** A message counts as shown once it stayed in the reading area this long in a visible, focused window. */
export const SHOWN_MS = 500;

/** Enough of a message is in the reading area: half of it, or half of the area for one taller than it. */
export function inReadingArea(entry: { intersectionRect: { height: number }; boundingClientRect: { height: number };
  rootBounds: { height: number } | null }): boolean {
  const own = entry.boundingClientRect.height;
  const area = entry.rootBounds?.height ?? own;
  return own > 0 && entry.intersectionRect.height >= Math.min(own, area) * 0.5;
}

/** A reply of `agent` in its chat: its text (tool steps, status lines and people's messages are not). */
export function isReply(message: { author: string; kind: string }, agent: string): boolean {
  return message.kind === "text" && message.author === `agent:${agent}`;
}

/** Which messages a chat has shown: each must stay in the reading area SHOWN_MS while the window is visible and
 * focused; time in a hidden or unfocused window does not count (the clock starts again when it is back). */
export class Reading {
  private since = new Map<number, number>();
  private seen = new Set<number>();
  private paused = false;
  private readonly now: () => number;
  constructor(now: () => number = Date.now) {
    this.now = now;
  }

  enter(seq: number) {
    if (!this.since.has(seq)) this.since.set(seq, this.now());
  }

  leave(seq: number) {
    this.since.delete(seq);
  }

  /** One look at the clock: the messages shown long enough so far. */
  tick(active: boolean): Set<number> {
    const now = this.now();
    if (!active) {
      this.paused = true;
      return this.seen;
    }
    if (this.paused) {
      for (const seq of this.since.keys()) this.since.set(seq, now);
      this.paused = false;
    }
    for (const [seq, at] of this.since) if (now - at >= SHOWN_MS) this.seen.add(seq);
    return this.seen;
  }

  reset() {
    this.since.clear();
    this.seen.clear();
    this.paused = false;
  }
}

/** Where the cursor may move: up to the highest message shown, but never past a reply the person was not shown
 * (one skipped while scrolling, one still off screen) — that reply stays new. `replies` are the reply seqs the
 * page holds, `shown` the seqs shown long enough. */
export function readUpTo(read: number, replies: number[], shown: Set<number>): number {
  let top = read;
  for (const seq of shown) if (seq > top) top = seq;
  for (const seq of [...replies].sort((a, b) => a - b)) {
    if (seq <= read) continue;
    if (!shown.has(seq)) return Math.max(read, Math.min(top, seq - 1));
  }
  return top;
}
