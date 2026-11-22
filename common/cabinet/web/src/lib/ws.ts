// The live channel (/ws): journal events of the subscribed cards (or all of them) and the inbox badge.
// It reconnects with an exponential backoff up to 30 s, then asks for the events after the last seq it knows
// (an event's or the hub's hello, so a page that saw no event still catches up) and restores its
// subscription. {"reload": true} means the hub cannot replay what the page missed. Close code 4401 means
// the session is gone: no retry.

import type { JournalEvent } from "./types";
import type { Chats } from "./unread";

/** A journal event as the hub sends it: {seq, ts, actor, kind, card, payload}. */
export type LiveEvent = JournalEvent;

export interface Badge {
  inbox: number;
  /** Incidents not resolved yet, for who reads the servers. */
  incidents?: number;
}

export interface WsOptions {
  onEvent: (event: LiveEvent) => void;
  onBadge: (badge: Badge) => void;
  /** The person's agent chats — cursors and unread replies. */
  onChats?: (chats: Chats) => void;
  onUnauthorized?: () => void;
  onState?: (connected: boolean) => void;
  onReload?: () => void;
  url?: string;
  socket?: (url: string) => WebSocket;
}

export interface Live {
  subscribeCard(id: string | null): void;
  subscribeAll(on: boolean): void;
  /** Ask the hub for the subscribed events after `after` (a snapshot's watermark). Not open yet: the next
   * open asks it, after the subscription. */
  replay(after: number): void;
  /** The network is back or the tab is shown: a dropped link reconnects now, not after its backoff. */
  wake(): void;
  lastSeq(): number;
  close(): void;
}

const MAX_DELAY = 30_000;
const CLOSE_SESSION = 4401;

export function backoff(attempt: number): number {
  return Math.min(1000 * 2 ** attempt, MAX_DELAY);
}

function defaultUrl(): string {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${location.host}/ws`;
}

export function connectWs(options: WsOptions): Live {
  const url = options.url ?? defaultUrl();
  const open = options.socket ?? ((u: string) => new WebSocket(u));
  let socket: WebSocket | null = null;
  let attempt = 0;
  let seq = 0;
  let known = false; // seq is a real position in the journal (an event or a hello came)
  let asked: number | null = null; // a replay asked while the link was not open: sent when it opens
  let card: string | null = null;
  let all = false;
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const send = (message: unknown) => {
    if (socket && socket.readyState === 1) socket.send(JSON.stringify(message));
  };

  // the server keeps `all` and the card set apart; a card page turns `all` off explicitly
  const subscription = () => (all ? { all: true } : { all: false, card: card ?? "" });

  function connect() {
    const current = open(url);
    socket = current;
    current.onopen = () => {
      attempt = 0;
      options.onState?.(true);
      if (all || card) send({ subscribe: subscription() });
      // what this page may have missed: since the last seq it saw, or since a snapshot's watermark asked
      // before the link opened (the first connection of a page that read its data over HTTP first)
      const from = known ? Math.min(seq, asked ?? seq) : asked;
      asked = null;
      if (from !== null) send({ after: from });
    };
    current.onmessage = (message: MessageEvent) => {
      let data: any;
      try {
        data = JSON.parse(String(message.data));
      } catch {
        return;
      }
      if (data?.event && typeof data.event.seq === "number") {
        seq = Math.max(seq, data.event.seq);
        known = true;
        options.onEvent(data.event as LiveEvent);
      } else if (data?.hello && typeof data.hello.seq === "number") {
        // the journal's position when this connection opened; what came before is replayed by `after`
        seq = Math.max(seq, data.hello.seq);
        known = true;
      } else if (data?.reload) {
        options.onReload?.();
      } else if (data?.badge) {
        options.onBadge(data.badge as Badge);
      } else if (data?.chats && typeof data.chats === "object") {
        options.onChats?.(data.chats as Chats);
      }
    };
    current.onclose = (event: CloseEvent) => {
      if (socket !== current) return;
      socket = null;
      if (stopped) return; // the page closed it: nothing was lost
      options.onState?.(false);
      if (event.code === CLOSE_SESSION) {
        stopped = true;
        options.onUnauthorized?.();
        return;
      }
      timer = setTimeout(connect, backoff(attempt++));
    };
  }

  connect();

  return {
    subscribeCard(id) {
      card = id;
      all = false;
      send({ subscribe: subscription() });
    },
    subscribeAll(on) {
      all = on;
      send({ subscribe: on ? { all: true } : { all: false } });
    },
    replay(after) {
      if (socket && socket.readyState === 1) send({ after });
      else asked = asked === null ? after : Math.min(asked, after);
    },
    wake() {
      if (stopped || socket) return;
      clearTimeout(timer);
      attempt = 0;
      connect();
    },
    lastSeq: () => seq,
    close() {
      stopped = true;
      clearTimeout(timer);
      socket?.close();
    },
  };
}
