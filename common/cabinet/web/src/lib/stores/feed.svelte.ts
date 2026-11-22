// The feed: newest first, older pages by `before`, newer events (live) by `after`. A filtered page may come
// back empty while the server scanned on; only a page that moved nowhere ends the feed.
import { apiGet } from "../api";
import { feedQuery } from "../feed";
import type { FeedPage, JournalEvent } from "../types";

const PAGE = 50;

export interface FeedFilter {
  project?: string;
  group?: string;
}

class Feed {
  events = $state<JournalEvent[]>([]);
  done = $state(false);
  loading = $state(false);
  private filter: FeedFilter = {};
  private before: number | null = null;
  private after = 0; // forward cursor includes scanned events excluded by the filter
  private token = 0;
  private ready = ""; // the filter whose first page is in; "": none

  async load(filter: FeedFilter) {
    const token = ++this.token;
    this.filter = filter;
    this.ready = "";
    this.events = [];
    this.done = false;
    this.before = null;
    this.after = 0;
    await this.page(token);
    if (token === this.token) this.ready = key(filter);
  }

  /** The screen's one read: the filter's first page until it is in, then what is newer. */
  async sync(filter: FeedFilter) {
    if (this.ready && this.ready === key(filter)) await this.newer();
    else await this.load(filter);
  }

  /** The next older page; nothing while one is loading or the feed has ended. */
  async more() {
    if (this.loading || this.done) return;
    await this.page(this.token);
  }

  private async page(token: number) {
    this.loading = true;
    const asked = this.before;
    try {
      const page = await apiGet<FeedPage>(
        feedQuery({ ...this.filter, limit: PAGE, ...(asked === null ? {} : { before: asked }) }),
      );
      if (token !== this.token) return;
      this.events = [...this.events, ...page.events.filter((e) => !this.has(e.seq))];
      this.before = page.before;
      if (asked === null) this.after = page.last;
      this.done = page.events.length === 0 && (asked === null || page.before === asked || (page.before ?? 0) <= 1);
    } finally {
      if (token === this.token) this.loading = false;
    }
  }

  /** What is new since the newest event shown (a live event arrived). */
  async newer() {
    const token = this.token;
    let cursor = this.after;
    // The cursor also advances over filtered events. Drain the gap on this resync, even if no further
    // live event arrives to trigger another read. Token checks also cover filter changes and sign-out.
    while (true) {
      const page = await apiGet<FeedPage>(feedQuery({ ...this.filter, limit: PAGE, after: cursor }));
      if (token !== this.token) return;
      const fresh = page.events.filter((e) => !this.has(e.seq)).sort((a, b) => b.seq - a.seq);
      if (fresh.length) this.events = [...fresh, ...this.events];
      if (page.last <= cursor) return;
      cursor = page.last;
      this.after = cursor;
      if (!(page.more ?? (page.events.length === PAGE || page.events.length === 0))) return;
    }
  }

  /** Sign-out: nothing of this person stays for the next one, not even a read on its way. */
  reset() {
    this.token++;
    this.filter = {};
    this.ready = "";
    this.events = [];
    this.done = false;
    this.loading = false;
    this.before = null;
    this.after = 0;
  }

  private has(seq: number): boolean {
    return this.events.some((e) => e.seq === seq);
  }
}

const key = (filter: FeedFilter) => JSON.stringify([filter.project ?? "", filter.group ?? ""]);

export const feed = new Feed();
