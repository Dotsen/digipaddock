// The app's one live connection while someone is signed in: the inbox badge, the link state and an event bus
// for the screens (a card page subscribes to its card, the feed and the board to everything). When the page
// may have missed something (the link came back, the hub could not replay, the tab was away, the network
// returned) the screens read their data again in place through onResync; drafts stay.
import { UNAUTHORIZED } from "./api";
import { applyChats, clearChats, refreshChats } from "./chats.svelte";
import { connectWs, type Live, type LiveEvent } from "./ws";

type Listener = (event: LiveEvent) => void;
export type Link = "connecting" | "online" | "reconnecting";

const AWAY_MS = 10_000; // a tab hidden longer than this resyncs when it is shown again

class LiveState {
  inbox = $state(0);
  incidents = $state(0); // not resolved yet: the menu's count
  link = $state<Link>("connecting");
  online = $state(typeof navigator === "undefined" ? true : navigator.onLine);
  get connected() {
    return this.link === "online";
  }
}

export const live = new LiveState();
const listeners = new Set<Listener>();
const resyncers = new Set<() => void>();
let channel: Live | null = null;
let card: string | null = null; // the card page's subscription
let watchers = 0; // screens that want every event
let hiddenAt = 0;

/** Every screen reads its data again (no page reload, drafts stay). */
export function resyncAll() {
  void refreshChats();
  resyncers.forEach((resync) => resync());
}

/** Run `resync` whenever the app resyncs; the returned function stops (use it as an $effect's cleanup). */
export function onResync(resync: () => void): () => void {
  resyncers.add(resync);
  return () => resyncers.delete(resync);
}

export function startLive() {
  if (channel) return;
  live.link = "connecting";
  channel = connectWs({
    onEvent: (event) => listeners.forEach((listener) => listener(event)),
    onBadge: (badge) => {
      live.inbox = badge.inbox;
      live.incidents = badge.incidents ?? 0;
    },
    onChats: applyChats,
    onState: (connected) => {
      const back = connected && live.link === "reconnecting";
      // a failed first connection is a lost link too: the strip says so and its opening resyncs
      live.link = connected ? "online" : "reconnecting";
      if (back) resyncAll();
    },
    onUnauthorized: () => dispatchEvent(new CustomEvent(UNAUTHORIZED)),
    // the page fell too far behind for a replay: read the data again in place
    onReload: resyncAll,
  });
  // screens may have asked before the channel existed
  if (watchers > 0) channel.subscribeAll(true);
  else if (card) channel.subscribeCard(card);
}

export function stopLive() {
  channel?.close();
  channel = null;
  live.link = "connecting";
  live.inbox = 0;
  live.incidents = 0;
  clearChats();
}

/** Ask the hub for the events after a snapshot's watermark: they may have passed before the subscription. */
export function replay(after: number) {
  channel?.replay(after);
}

/** Listen to live events; the returned function stops listening (use it as an $effect's cleanup). */
export function onLiveEvent(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** One card's events (its page); null when the page closes. */
export function subscribeCard(id: string | null) {
  card = id;
  channel?.subscribeCard(id);
}

/** Every visible event (inbox, board, feed) while at least one screen asks; the returned function stops. */
export function watchAll(): () => void {
  watchers += 1;
  if (watchers === 1) channel?.subscribeAll(true);
  let done = false;
  return () => {
    if (done) return;
    done = true;
    watchers -= 1;
    if (watchers === 0) channel?.subscribeAll(false);
  };
}

if (typeof window !== "undefined") {
  addEventListener("online", () => {
    live.online = true;
    channel?.wake();
    resyncAll();
  });
  addEventListener("offline", () => (live.online = false));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") hiddenAt = Date.now();
    else {
      channel?.wake();
      if (hiddenAt && Date.now() - hiddenAt > AWAY_MS) resyncAll();
      hiddenAt = 0;
    }
  });
}
