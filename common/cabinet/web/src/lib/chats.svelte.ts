// The person's agent chats and their unread replies: the server's counts, live over the WebSocket
// ({"chats": ...}) and read again on every resync. A page reports what it showed with reportRead; a failed
// report keeps the marks and is sent again, a late answer never moves the cursor back. A key is an agent
// thread's (lib/spaces: the agent for its chat, `<agent>/monitor` for "Monitor").
import { ApiError, apiGet, apiPost } from "./api";
import { agentUnread, spaceBase } from "./spaces";
import { mergeChats, type ChatState, type Chats } from "./unread";

const RETRY_MS = 3000;

class ChatsState {
  all = $state<Chats>({});
  unread(agent: string): number {
    return this.all[agent]?.unread ?? 0;
  }
  /** The unread replies of every thread of the agent (its chat and "Monitor"). */
  total(agent: string): number {
    return agentUnread(this.all, agent);
  }
  of(agent: string): ChatState | undefined {
    return this.all[agent];
  }
}

export const chats = new ChatsState();
const pending = new Map<string, number>(); // agent → the highest seq still to report
const sending = new Set<string>();
let generation = 0; // a sign-out drops what was in flight

export function applyChats(next: Chats) {
  chats.all = mergeChats(chats.all, next);
}

export async function refreshChats() {
  const mine = generation;
  try {
    const found = await apiGet<Chats>("/api/chats");
    if (mine === generation) applyChats(found);
  } catch {
    // the next resync or WebSocket message brings them
  }
}

/** The page showed `agent`'s chat up to `seq` (a visible, focused window): move the person's cursor. */
export function reportRead(agent: string, seq: number) {
  const known = chats.of(agent);
  if (known && seq <= known.read) return;
  pending.set(agent, Math.max(pending.get(agent) ?? 0, seq));
  void flush(agent);
}

async function flush(agent: string) {
  if (sending.has(agent)) return;
  const seq = pending.get(agent);
  if (seq === undefined) return;
  sending.add(agent);
  const mine = generation;
  try {
    const state = await apiPost<ChatState>(`${spaceBase(agent)}/read`, { seq });
    if (mine !== generation) return;
    if (pending.get(agent) === seq) pending.delete(agent);
    applyChats({ ...chats.all, [agent]: state });
  } catch (exc) {
    if (mine !== generation) return;
    if (exc instanceof ApiError && exc.status > 0 && exc.status < 500) {
      pending.delete(agent); // refused (no such agent, no permission): repeating changes nothing
      return;
    }
    setTimeout(() => void flush(agent), RETRY_MS); // the network or the server: the marks stay until it lands
    return;
  } finally {
    sending.delete(agent);
  }
  if (pending.has(agent)) void flush(agent);
}

/** "Mark the chat as read": the snapshot the page holds, not what comes after it. */
export function readAll(agent: string, last: number) {
  reportRead(agent, last);
}

/** Sign-out: nothing of the previous person stays. */
export function clearChats() {
  generation += 1;
  pending.clear();
  chats.all = {};
}
