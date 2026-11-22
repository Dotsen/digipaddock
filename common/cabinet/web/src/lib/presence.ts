// Presence in an agent chat: while this page shows the chat in a visible, focused window it renews a short
// lease on the server every RENEW_MS, so the agent's replies need no push to this person; hidden, unfocused or
// closed — it says so (a crashed tab's lease simply runs out). Another tab, a hidden chat or another person
// suppresses nothing.
import { apiPost } from "./api";
import { spaceBase } from "./spaces";

export const RENEW_MS = 10_000; // the server's lease is 25 s (chats.PRESENCE_LEASE)

const KEY = "kd.device";

/** This tab's id (a page in front of the person, not a browser): kept for the tab's life. */
export function deviceId(storage: Pick<Storage, "getItem" | "setItem"> | undefined = globalThis.sessionStorage): string {
  const held = storage?.getItem(KEY);
  if (held && /^[A-Za-z0-9-]{8,64}$/.test(held)) return held;
  const made = `tab-${Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("")}`;
  storage?.setItem(KEY, made);
  return made;
}

export interface PresenceDeps {
  active: () => boolean;
  send: (on: boolean) => Promise<unknown>;
  every: (fn: () => void, ms: number) => () => void;
  listen: (fn: () => void) => () => void;
}

/** Hold presence in `agent`'s chat while the window is active; the returned function stops (and says so). */
export function holdPresence(agent: string, deps: PresenceDeps = browserDeps(agent)): () => void {
  let on = false;
  const tell = (next: boolean) => {
    if (next === on && !next) return; // nothing to take back
    on = next;
    void deps.send(next).catch(() => undefined); // a lost renewal only means a push may come
  };
  const look = () => tell(deps.active());
  look();
  const stopTimer = deps.every(() => {
    if (deps.active()) tell(true); // a renewal
    else tell(false);
  }, RENEW_MS);
  const stopListening = deps.listen(look);
  return () => {
    stopTimer();
    stopListening();
    tell(false);
  };
}

function browserDeps(agent: string): PresenceDeps {
  const device = deviceId();
  const path = `${spaceBase(agent)}/presence`; // `agent` is a thread's key: its chat or "Monitor"
  return {
    active: () => document.visibilityState === "visible" && document.hasFocus(),
    send: (on) => apiPost(path, { device, on }),
    every: (fn, ms) => {
      const timer = setInterval(fn, ms);
      return () => clearInterval(timer);
    },
    listen: (fn) => {
      document.addEventListener("visibilitychange", fn);
      addEventListener("focus", fn);
      addEventListener("blur", fn);
      addEventListener("pagehide", fn);
      return () => {
        document.removeEventListener("visibilitychange", fn);
        removeEventListener("focus", fn);
        removeEventListener("blur", fn);
        removeEventListener("pagehide", fn);
      };
    },
  };
}
